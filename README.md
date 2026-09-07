# Meme Prospect Telegram Bot

A 24/7 meme-coin prospect scanner designed for **5-minute monitoring** and Telegram alerts.

## What it does

- Polls **Birdeye New Listing** when a Birdeye API key is configured.
- Uses **DEX Screener** to enrich candidates with liquidity, market cap, age, 5m/1h volume, buys/sells, price change and socials.
- Keeps SQLite snapshots so it can compare the current 5-minute scan with previous scans.
- Scores each token from 0-100.
- Sends an alert only when the token meets the configured threshold (default **85/100**).
- Applies security/risk gates before alerts when Birdeye security data is available.
- Deduplicates alerts so the same token does not spam Telegram.
- Telegram commands:
  - `/start` — pair the running scanner with your Telegram chat
  - `/status` — show scanner settings
  - `/scan` — run a scan immediately and show the strongest candidates
  - `/top` — show the latest stored candidates
  - `/help` — command help

> This is a scanner/alert bot, not an auto-buy bot. Meme coins are extremely high risk. A high score is not a guarantee of profit or safety.

## Fast setup

### 1. Create the Telegram bot
In Telegram:
1. Open **@BotFather**.
2. Send `/newbot`.
3. Choose a name and username.
4. Copy the bot token.

### 2. Configure
Copy `.env.example` to `.env`.

Set:

```env
TELEGRAM_BOT_TOKEN=YOUR_TOKEN_FROM_BOTFATHER
BIRDEYE_API_KEY=YOUR_BIRDEYE_KEY
```

You can leave `TELEGRAM_CHAT_ID` blank.

### 3. Run

```bash
python -m venv .venv
```

Windows:
```bash
.venv\Scripts\activate
```

macOS/Linux:
```bash
source .venv/bin/activate
```

Then:

```bash
pip install -r requirements.txt
python -m app.main
```

### 4. Pair your Telegram
Open your newly created Telegram bot and send:

```text
/start
```

The scanner stores that private chat ID locally and uses it for future alerts.

## Recommended hosting
To scan every five minutes continuously, this code must run on an always-on computer/VPS/cloud service. A Dockerfile is included.

## Discovery coverage

**With Birdeye key:** uses Birdeye's dedicated recently listed-token endpoint for each configured chain, with meme-platform discovery enabled on Solana.

**Without Birdeye key:** falls back to DEX Screener's latest token profiles and boosts as candidate discovery. This fallback can miss fresh launches, so Birdeye is strongly recommended.

## Score model

The 100-point prospect score rewards:
- sensible early market-cap range
- adequate and rising liquidity
- liquidity/market-cap health
- strong 5-minute volume relative to liquidity
- buy/sell imbalance with meaningful transaction count
- positive but not absurdly extended momentum
- young pair age
- volume/buyer acceleration versus previous scan
- project/social metadata

The scanner applies deductions/gates for:
- very low liquidity
- extreme or already-overextended short-term price moves
- severe sell imbalance
- security flags when available
- suspicious token-authority or honeypot-style risk signals when reported by the security provider

## Important
Never paste exchange seed phrases, private keys, or withdrawal-enabled exchange API keys into this project.
