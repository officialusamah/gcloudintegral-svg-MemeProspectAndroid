from __future__ import annotations
import asyncio, html, time
from .config import Config
from .db import Store
from .providers import MarketProviders
from .scanner import Scanner
from .telegram import Telegram

HELP = """<b>Meme Prospect Bot</b>

Commands:
/start — pair this chat
/status — scanner status
/scan — scan immediately
/top — latest strongest stored candidates
/help — show this help"""

def score_icon(score: int) -> str:
    if score >= 85:
        return "🟢"
    if score >= 60:
        return "🟡"
    return "🔴"

def score_label(score: int) -> str:
    if score >= 85:
        return "STRONG PROSPECT"
    if score >= 60:
        return "EARLY WATCH"
    return "LOW SCORE"

def fmt_top(items):
    if not items:
        return "No candidates stored yet."
    lines = ["<b>Strongest recent candidates</b>"]
    for i, x in enumerate(items[:5], 1):
        s, r = x.snapshot, x.result
        lines.append(
            f"{i}. {score_icon(r.score)} <b>{html.escape(s.token_symbol)}</b> — "
            f"{r.score}/100 ({score_label(r.score)}) | "
            f"MC ${s.market_cap:,.0f} | Liq ${s.liquidity_usd:,.0f} | "
            f"5m {s.price_change_m5:+.1f}%"
        )
    return "\n".join(lines)

async def command_loop(tg: Telegram, scanner: Scanner, cfg: Config, store: Store):
    while True:
        try:
            updates = await tg.updates()
            for u in updates:
                tg.offset = max(tg.offset, int(u.get("update_id", 0)) + 1)
                msg = u.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = str(chat.get("id") or "")
                text = str(msg.get("text") or "").strip()
                if not chat_id or not text:
                    continue

                cmd = text.split()[0].split("@")[0].lower()
                if cmd == "/start":
                    if tg.accept_chat(chat_id):
                        await tg.send(
                            "✅ <b>Paired.</b>\n"
                            "The scanner can now send prospect alerts to this chat.\n\n" + HELP,
                            chat_id,
                        )
                    else:
                        await tg.send("This scanner is already paired to another chat.", chat_id)
                    continue

                if not tg.accept_chat(chat_id):
                    continue

                if cmd == "/help":
                    await tg.send(HELP)

                elif cmd == "/status":
                    source = "Birdeye new listings + DEX Screener" if cfg.birdeye_api_key else "DEX Screener fallback only"
                    ago = int(time.time()) - scanner.last_scan_ts if scanner.last_scan_ts else None
                    await tg.send(
                        f"<b>Scanner status</b>\n"
                        f"Interval: {cfg.scan_interval_seconds}s\n"
                        f"🟡 Early Watch: 60/100\n"
                        f"🟢 Strong Prospect: 85/100\n"
                        f"Chains: {', '.join(cfg.chains)}\n"
                        f"Discovery: {source}\n"
                        f"Last scan: {ago if ago is not None else 'not yet'}s ago\n"
                        f"Last candidate count: {scanner.last_count}"
                    )

                elif cmd == "/scan":
                    await tg.send("🔎 Running a scan now…")
                    top = await scanner.run_once(send_alerts=True)
                    await tg.send(fmt_top(top))

                elif cmd == "/top":
                    rows = store.latest_top(5)
                    if not rows:
                        await tg.send("No candidates stored yet.")
                    else:
                        lines = ["<b>Latest stored scores</b>"]
                        for i, r in enumerate(rows, 1):
                            score = int(r["score"])
                            lines.append(
                                f"{i}. {score_icon(score)} <b>{html.escape(str(r['token_symbol']))}</b> — "
                                f"{score}/100 ({score_label(score)}) | "
                                f"MC ${float(r['market_cap'] or 0):,.0f} | "
                                f"Liq ${float(r['liquidity_usd'] or 0):,.0f}"
                            )
                        await tg.send("\n".join(lines))

        except Exception as e:
            print("telegram polling error:", repr(e))
            await asyncio.sleep(5)

async def main():
    cfg = Config()
    if not cfg.telegram_bot_token:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN.")

    store = Store(cfg.db_path)
    market = MarketProviders(cfg.birdeye_api_key)
    tg = Telegram(cfg.telegram_bot_token, store, cfg.telegram_chat_id)
    scanner = Scanner(cfg, store, market, tg)

    print("Meme Prospect Telegram Bot starting")
    print(f"interval={cfg.scan_interval_seconds}s threshold={cfg.alert_score_threshold} chains={cfg.chains}")

    if not cfg.birdeye_api_key:
        print("WARNING: BIRDEYE_API_KEY missing; fresh-launch coverage is limited.")

    if not tg.chat_id:
        print("Telegram chat not paired yet. Send /start to your bot after it starts.")

    try:
        await asyncio.gather(scanner.loop(), command_loop(tg, scanner, cfg, store))
    finally:
        await market.close()
        await tg.close()

if __name__ == "__main__":
    asyncio.run(main())
