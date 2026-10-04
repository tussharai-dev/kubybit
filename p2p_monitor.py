"""
P2P USDT/INR merchant monitor for Bybit and KuCoin.

Alerts (Windows pop-up) when a merchant matches ALL of:
  1. Has at least one SELL ad (merchant sells USDT) that accepts UPI.
  2. Every SELL ad of theirs that uses IMPS / RTGS / NEFT / bank transfer has a max limit <= 1,00,000 INR.
  3. Has a BUY ad (merchant buys USDT - you sell to them) priced ABOVE 100 INR
     that accepts UPI, IMPS or bank transfer, with a max limit of at least 1,00,000 INR
     (ads above 1 lakh are starred and listed first).

Run:  python p2p_monitor.py          (Ctrl+C or close the window to stop)
Log:  p2p_monitor.log (same folder)

Telegram alerts:
  1. Put your bot token in p2p_monitor_config.json  ->  {"telegram_bot_token": "..."}
  2. Send any message (e.g. /start) to your bot in Telegram
  3. Run:  python p2p_monitor.py --setup-telegram   (saves your chat id, sends a test message)

Cloud mode (GitHub Actions, no pop-ups), configured by environment variables:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  - override the config file
  RUN_MINUTES   - exit after this many minutes (one Actions job)
  STOP_AFTER    - YYYY-MM-DD (UTC); exit immediately on/after this date
  STATE_FILE    - JSON file to remember already-alerted ads between jobs
"""
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
WINDOWS = sys.platform == "win32"

INTERVAL_SEC = int(os.environ.get("INTERVAL_SEC", 60))
MIN_BUY_PRICE = 100.0
MIN_BUY_MAX = 100000.0  # merchant's buy ad must let you sell at least this much in one order
RESET_ID = "2026-10-04b"  # change this to make the next cloud run resend every current match
SELL_BANK_MAX = 100000.0

UA = {"User-Agent": "Mozilla/5.0", "content-type": "application/json"}
LOG = Path(__file__).with_name("p2p_monitor.log")
CONFIG = Path(__file__).with_name("p2p_monitor_config.json")
UPI_RE = re.compile(r"\bUPI\b", re.I)
BANK_RE = re.compile(r"IMPS|RTGS|NEFT|BANK.?TRANSFER", re.I)
PAYOUT_RE = re.compile(r"\bUPI\b|IMPS|BANK.?TRANSFER", re.I)  # what you accept to receive


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def http(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=UA, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


# ---------------- Bybit ----------------
_bybit_pay = {}


def bybit_pay_names():
    if not _bybit_pay:
        j = http("https://api2.bybit.com/fiat/otc/configuration/queryAllPaymentList", {})
        for p in j["result"]["paymentConfigVo"]:
            _bybit_pay[str(p["paymentType"])] = p["paymentName"].strip()
    return _bybit_pay


def bybit_ads(side):
    names = bybit_pay_names()
    out, page = [], 1
    while page <= 50:
        j = http("https://api2.bybit.com/fiat/otc/item/online", {
            "tokenId": "USDT", "currencyId": "INR", "side": side, "size": "20", "page": str(page),
            "amount": "", "authMaker": False, "canTrade": False, "payment": []})
        items = j.get("result", {}).get("items") or []
        for i in items:
            out.append({
                "id": i["id"], "merchant": i["userMaskId"], "name": i["nickName"],
                "price": float(i["price"]), "min": float(i["minAmount"]), "max": float(i["maxAmount"]),
                "pays": [names.get(str(p), str(p)) for p in i["payments"]],
                "stats": f"{i['recentOrderNum']} orders/30d, {i['recentExecuteRate']}% completion",
            })
        if len(items) < 20:
            break
        page += 1
    return out


def fetch_bybit():
    return bybit_ads("1"), bybit_ads("0")  # side 1 = merchant sells, 0 = merchant buys


# ---------------- KuCoin ----------------
def kucoin_ads(side):
    out, page = [], 1
    while page <= 30:
        j = http(f"https://www.kucoin.com/_api/otc/ad/list?currency=USDT&side={side}&legal=INR"
                 f"&page={page}&pageSize=50&status=PUTUP&lang=en_US")
        for i in j.get("items") or []:
            price = float(i["floatPrice"])
            mx = min(float(i["limitMaxQuote"]), float(i["currencyBalanceQuantity"]) * price)
            out.append({
                "id": i["id"], "merchant": i["nickName"], "name": i["nickName"],
                "price": price, "min": float(i["limitMinQuote"]), "max": mx,
                "pays": sorted({p["payTypeNameEn"] or p["payTypeCode"] for p in i["adPayTypes"]}),
                "stats": f"{i['dealOrderNum']} trades all-time ({i['dealOrderRate']}), {i.get('lastActiveDesc') or ''}",
            })
        if page >= (j.get("totalPage") or 1):
            break
        page += 1
    return out


def fetch_kucoin():
    return kucoin_ads("SELL"), kucoin_ads("BUY")


# ---------------- Matching ----------------
def has(pays, rx):
    return any(rx.search(p) for p in pays)


def find_matches(platform, sells, buys):
    by_m = {}
    for a in sells:
        by_m.setdefault(a["merchant"], {"s": [], "b": []})["s"].append(a)
    for a in buys:
        by_m.setdefault(a["merchant"], {"s": [], "b": []})["b"].append(a)

    matches = []
    for m in by_m.values():
        s, b = m["s"], m["b"]
        if not s or not b:
            continue
        if not any(has(a["pays"], UPI_RE) for a in s):
            continue
        if any(has(a["pays"], BANK_RE) and a["max"] > SELL_BANK_MAX for a in s):
            continue
        for a in b:
            if a["price"] > MIN_BUY_PRICE and a["max"] >= MIN_BUY_MAX and has(a["pays"], PAYOUT_RE):
                matches.append((platform, a, s))
    return matches


def describe(platform, buy, sells):
    sell_txt = "; ".join(f"Rs{a['price']:g} ({'/'.join(a['pays'])}, Rs{a['min']:,.0f}-{a['max']:,.0f})" for a in sells)
    star = "⭐ " if buy["max"] > MIN_BUY_MAX else ""
    return (f"{star}[{platform}] {buy['name']}\n"
            f"  BUYS your USDT at Rs{buy['price']:g}  |  limit Rs{buy['min']:,.0f} - {buy['max']:,.0f}\n"
            f"  Pays you via: {', '.join(buy['pays'])}\n"
            f"  Sells USDT as: {sell_txt}\n"
            f"  {buy['stats']}")


def popup(title, text):
    if not WINDOWS:
        return
    import ctypes
    import winsound

    def show():
        # MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x0 | 0x40 | 0x10000 | 0x40000)
    threading.Thread(target=show, daemon=True).start()
    try:
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


# ---------------- Telegram ----------------
def load_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def telegram_api(token, method, params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode())
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


_found_chat = {}


def _chat_file():
    path = os.environ.get("STATE_FILE")
    return Path(path).with_name("chat_id.txt") if path else None


def _chat_from_updates(token):
    """No chat id given: use whoever last messaged the bot (send it /start once).
    Telegram only keeps updates ~24h, so the id is remembered next to STATE_FILE."""
    if not token:
        return None
    if token in _found_chat:
        return _found_chat[token]
    f = _chat_file()
    if f and f.exists() and f.read_text().strip():
        _found_chat[token] = f.read_text().strip()
        return _found_chat[token]
    try:
        ups = telegram_api(token, "getUpdates", {}).get("result", [])
    except Exception as e:
        log(f"Telegram getUpdates failed: {e}")
        return None
    chats = [u["message"]["chat"]["id"] for u in ups if "message" in u]
    if not chats:
        log("Telegram: no chat found yet - send /start to your bot.")
        return None
    _found_chat[token] = str(chats[-1])
    if f:
        f.write_text(_found_chat[token])
    log(f"Telegram: using chat id {chats[-1]}.")
    return _found_chat[token]


def telegram_ready():
    """True if alerts can go out (Telegram set up), or we're local with pop-ups."""
    cfg = load_config()
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("telegram_bot_token")
    if not token:
        return WINDOWS
    chat = os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("telegram_chat_id") or _chat_from_updates(token)
    return bool(chat) or WINDOWS


def telegram_send(text):
    cfg = load_config()
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("telegram_bot_token")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("telegram_chat_id") or _chat_from_updates(token)
    if not token or not chat:
        return False
    chunks, cur = [], ""
    for part in text.split("\n\n"):  # Telegram limit is 4096 chars per message
        if len(cur) + len(part) + 2 > 3900:
            chunks.append(cur)
            cur = ""
        cur += part + "\n\n"
    chunks.append(cur)
    ok = True
    for c in chunks:
        try:
            telegram_api(token, "sendMessage", {"chat_id": chat, "text": c.strip()})
        except Exception as e:
            log(f"Telegram send failed: {e}")
            ok = False
    return ok


def setup_telegram():
    cfg = load_config()
    token = cfg.get("telegram_bot_token")
    if not token:
        print(f"Add your bot token to {CONFIG} as {{\"telegram_bot_token\": \"...\"}} first.")
        return
    updates = telegram_api(token, "getUpdates", {}).get("result", [])
    chats = [u["message"]["chat"] for u in updates if "message" in u]
    if not chats:
        print("No messages found. Open your bot in Telegram, send /start, then run this again.")
        return
    cfg["telegram_chat_id"] = chats[-1]["id"]
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    telegram_send("P2P monitor connected. You'll get merchant alerts here.")
    print(f"Saved chat id {chats[-1]['id']} for {chats[-1].get('first_name') or chats[-1].get('title')}. Test message sent.")


def load_state():
    path = os.environ.get("STATE_FILE")
    if not path:
        return set()
    try:
        return {tuple(x) for x in json.loads(Path(path).read_text(encoding="utf-8"))}
    except Exception:
        return set()


def save_state(seen):
    path = os.environ.get("STATE_FILE")
    if path:
        Path(path).write_text(json.dumps(sorted(seen)), encoding="utf-8")


def main():
    stop_after = os.environ.get("STOP_AFTER")
    if stop_after and datetime.now(timezone.utc).date().isoformat() >= stop_after:
        log(f"STOP_AFTER {stop_after} reached - not running.")
        return
    run_minutes = float(os.environ.get("RUN_MINUTES", 0))
    deadline = time.time() + run_minutes * 60 if run_minutes else None

    log(f"Monitor started. Checking Bybit + KuCoin every {INTERVAL_SEC}s. Buy price > {MIN_BUY_PRICE}, buy limit >= {MIN_BUY_MAX:,.0f}.")
    if not (os.environ.get("TELEGRAM_BOT_TOKEN") or load_config().get("telegram_bot_token")):
        log("Telegram not configured - Windows pop-ups only. Run with --setup-telegram to enable.")
    elif "--test-telegram" in sys.argv:
        telegram_send("✅ P2P monitor is running in the cloud. Alerts will arrive here.")
    seen = load_state()  # (platform, ad id, price) already alerted
    state = os.environ.get("STATE_FILE")
    delivered = Path(state).with_name("delivered.txt") if state else None
    if delivered and (not delivered.exists() or delivered.read_text().strip() != RESET_ID):
        seen = set()  # nothing delivered since the last reset, so don't skip anything
    failing = set()
    while deadline is None or time.time() < deadline:
        found = []
        for platform, fetch in (("Bybit", fetch_bybit), ("KuCoin", fetch_kucoin)):
            try:
                found += find_matches(platform, *fetch())
                if platform in failing:
                    failing.discard(platform)
                    telegram_send(f"✅ {platform} is reachable again.")
            except Exception as e:
                log(f"{platform} fetch failed: {e}")
                if platform not in failing:
                    failing.add(platform)
                    telegram_send(f"⚠️ P2P monitor can't reach {platform}: {e}")
        current = {(p, b["id"], b["price"]) for p, b, _ in found}
        new = [(p, b, s) for p, b, s in found if (p, b["id"], b["price"]) not in seen]
        if new and not telegram_ready():
            # Hold alerts until Telegram is reachable, so the first message isn't lost.
            log(f"{len(new)} match(es) waiting for Telegram setup.")
            new = []
            current = set(seen)
        if new:
            new.sort(key=lambda x: (x[1]["max"] <= MIN_BUY_MAX, -x[1]["price"]))  # above 1 lakh first
            body = "\n\n".join(describe(*x) for x in new)
            log(f"{len(new)} new match(es):\n{body}")
            shown = "\n\n".join(describe(*x) for x in new[:6])
            if len(new) > 6:
                shown += f"\n\n...and {len(new) - 6} more (see p2p_monitor.log)"
            popup("P2P: merchant you may want to trade with", shown)
            sent = telegram_send(f"🔔 Merchant(s) you may want to trade with ({len(new)} new)\n\n{body}")
            if sent and delivered:
                delivered.write_text(RESET_ID)
        else:
            log(f"No new matches ({len(current)} active).")
        # Keep ads still listed; ads that disappear are forgotten so they re-alert if they come back.
        # If a platform failed this round, keep its old entries so a blip doesn't re-alert everything.
        seen = current | {k for k in seen if k[0] in failing}
        save_state(seen)
        if deadline is not None and time.time() + INTERVAL_SEC >= deadline:
            break
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    if "--setup-telegram" in sys.argv:
        setup_telegram()
    else:
        main()
