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
/status — scanner + paper trader status
/scan — scan immediately
/top — latest strongest stored candidates
/performance — performance of recent prospect alerts
/positions — open paper positions
/paperstats — paper trader P/L summary
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

def fmt_price(value: float) -> str:
    value = float(value or 0)
    if value >= 1:
        return f"${value:,.4f}"
    if value >= 0.01:
        return f"${value:.6f}"
    return f"${value:.10f}".rstrip("0").rstrip(".")

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

def fmt_performance(rows):
    if not rows:
        return "<b>Paper performance</b>\nNo tracked prospect alerts with price data yet."
    lines = [
        "<b>📊 Prospect alert performance</b>",
        "<i>Measured from alert price; no trades are executed by this report.</i>",
    ]
    for i, r in enumerate(rows, 1):
        ret = r.get("return_pct")
        best = r.get("best_return_pct")
        icon = "⚪" if ret is None else ("🟢" if ret >= 0 else "🔴")
        ret_text = "n/a" if ret is None else f"{ret:+.1f}%"
        best_text = "n/a" if best is None else f"{best:+.1f}%"
        lines.append(
            f"\n{i}. {icon} <b>{html.escape(str(r.get('token_symbol') or '?'))}</b>\n"
            f"Alert score: {int(r.get('score') or 0)}/100\n"
            f"Return: <b>{ret_text}</b> | Best seen: <b>{best_text}</b>"
        )
    return "\n".join(lines)

def fmt_positions(rows):
    if not rows:
        return "<b>🧪 Paper positions</b>\nNo open simulated positions."
    lines = ["<b>🧪 Open paper positions</b>"]
    for i, p in enumerate(rows, 1):
        entry = float(p["entry_price"] or 0)
        last = float(p["last_price"] or 0)
        ret = ((last / entry) - 1) * 100 if entry and last else 0
        icon = "🟢" if ret >= 0 else "🔴"
        lines.append(
            f"\n{i}. {icon} <b>{html.escape(str(p['token_symbol'] or '?'))}</b>\n"
            f"Entry score: {int(p['entry_score'])}/100 | Last score: {int(p['last_score'] or 0)}/100\n"
            f"Return: <b>{ret:+.1f}%</b> | Remaining: <b>{float(p['remaining_percent']):.0f}%</b>\n"
            f"Entry: {fmt_price(entry)} | Last: {fmt_price(last)}"
        )
    return "\n".join(lines)

def fmt_paperstats(summary):
    realized = float(summary["realized_pnl_usd"] or 0)
    unrealized = float(summary["unrealized_pnl_usd"] or 0)
    total = realized + unrealized
    icon = "🟢" if total >= 0 else "🔴"
    return (
        "<b>🧪 Paper Trader Summary</b>\n"
        f"Open positions: <b>{len(summary['open'])}</b>\n"
        f"Closed positions: <b>{int(summary['closed_count'])}</b>\n"
        f"Realized P/L: <b>${realized:+,.2f}</b>\n"
        f"Unrealized P/L: <b>${unrealized:+,.2f}</b>\n"
        f"{icon} Combined P/L: <b>${total:+,.2f}</b>\n\n"
        "ℹ️ Simulation only — no wallet funds are used."
    )

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
                    paper_chain = "Solana only" if cfg.paper_solana_only else "all supported chains"
                    await tg.send(
                        f"<b>Scanner status</b>\n"
                        f"Interval: {cfg.scan_interval_seconds}s\n"
                        f"🟡 Early Watch: {cfg.early_watch_threshold}/100\n"
                        f"🟢 Strong Prospect: {cfg.alert_score_threshold}/100\n"
                        f"🟢🚀 Upgrade alerts: ON\n"
                        f"🚀 Rapid acceleration: ON\n"
                        f"📊 Alert tracking: ON\n\n"
                        f"<b>🧪 Paper Trader</b>\n"
                        f"Status: {'ON' if cfg.paper_trade_enabled else 'OFF'}\n"
                        f"Trading universe: {paper_chain}\n"
                        f"Auto-buy score: {cfg.paper_buy_score}/100\n"
                        f"Position size: ${cfg.paper_position_usd:,.2f}\n"
                        f"Max open positions: {cfg.paper_max_open_positions}\n"
                        f"TP1: +{cfg.paper_tp1_percent:.0f}% → sell {cfg.paper_tp1_sell_percent:.0f}%\n"
                        f"TP2: +{cfg.paper_tp2_percent:.0f}% → sell {cfg.paper_tp2_sell_percent:.0f}%\n"
                        f"Stop loss: -{cfg.paper_stop_loss_percent:.0f}%\n\n"
                        f"Chains scanned: {', '.join(cfg.chains)}\n"
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

                elif cmd == "/performance":
                    await tg.send(fmt_performance(store.recent_performance(5)))

                elif cmd == "/positions":
                    await tg.send(fmt_positions(store.open_paper_positions()))

                elif cmd == "/paperstats":
                    await tg.send(fmt_paperstats(store.paper_summary()))

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
    print(f"paper_trader={'ON' if cfg.paper_trade_enabled else 'OFF'} position=${cfg.paper_position_usd}")

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
