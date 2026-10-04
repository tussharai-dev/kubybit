# P2P merchant monitor (cloud)

Checks Bybit and KuCoin USDT/INR P2P every 60 seconds on GitHub Actions, and sends a Telegram alert when a merchant:
- sells USDT with UPI, with every IMPS/RTGS/NEFT/bank-transfer sell ad at or below ₹1,00,000, and
- buys USDT above ₹100, paying by UPI, IMPS or bank transfer.

## Setup
1. **Telegram bot:** in Telegram, message **@BotFather**, send `/newbot` and copy the token. Then open your new bot and send it `/start`.
2. **GitHub repo:** create a new **public** repository (public repos get unlimited free Actions minutes). Upload everything in this folder, including the `.github` folder.
3. **Secrets:** go to repo **Settings → Secrets and variables → Actions → New repository secret**, add `TELEGRAM_BOT_TOKEN` and paste in the token.
   - `TELEGRAM_CHAT_ID` is optional. Without it, the bot messages whoever last sent it `/start`.
4. **Stop date:** in the same page under the **Variables** tab, add `STOP_AFTER` with the date one week out, e.g. `2026-10-11`.
5. **Start it:** go to **Actions → P2P merchant monitor → Run workflow**. You should get "✅ P2P monitor is running in the cloud" on Telegram, followed by the first alerts.

After that it runs on its own, 24/7, until `STOP_AFTER`. To stop early, open **Actions → P2P merchant monitor → ⋯ → Disable workflow**.

## Notes
- If an exchange blocks GitHub's servers, you'll get a "⚠️ can't reach …" message on Telegram.
- Alerts are only sent for new ads or price changes. That memory is kept between runs.
- Settings live at the top of `p2p_monitor.py`: `MIN_BUY_PRICE` and `SELL_BANK_MAX`.
