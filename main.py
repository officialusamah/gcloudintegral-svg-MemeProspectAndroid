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
/wallet — connected Solana wallet + SOL balance
/livestatus — live-trading safety status
/livepositions — open real positions
/starttrading CONFIRM — arm real trading
/stoptrading — emergency pause for all real trades
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

async def command_loop(tg: Telegram, scanner: Scanner, cfg: Config, store: Store, market: MarketProviders):
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
                        f"<b>👛 Wallet</b>\n"
                        f"Connected: {'YES' if cfg.wallet_public_address else 'NO'}\n"
                        f"Live master: {'ON' if cfg.live_trading_enabled else 'OFF'}\n"
                        f"Telegram live lock: {'PAUSED' if scanner.live.is_paused() else 'ARMED'}\n\n"
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

                elif cmd == "/wallet":
                    if not cfg.wallet_public_address:
                        await tg.send(
                            "<b>👛 Wallet</b>\nNo wallet public address is configured."
                        )
                    else:
                        try:
                            sol = await market.solana_wallet_balance(
                                cfg.wallet_public_address, cfg.solana_rpc_url
                            )
                            short = (
                                cfg.wallet_public_address[:6]
                                + "…"
                                + cfg.wallet_public_address[-6:]
                            )
                            signer = scanner.live.signer_status()
                            signer_text = (
                                "NOT INSTALLED"
                                if not signer["installed"]
                                else ("MATCHED ✅" if signer["matches"] else "MISMATCH / INVALID ❌")
                            )
                            await tg.send(
                                f"<b>👛 Solana Wallet</b>\n"
                                f"Address: <code>{html.escape(cfg.wallet_public_address)}</code>\n"
                                f"Short: <b>{html.escape(short)}</b>\n"
                                f"SOL balance: <b>{sol:.6f} SOL</b>\n"
                                f"Signer: <b>{signer_text}</b>\n"
                                f"Live trading: <b>{'ARMED ⚠️' if scanner.live.execution_ready() else 'OFF / PAUSED 🔒'}</b>\n"
                                f"Paper trading: <b>{'ON' if cfg.paper_trade_enabled else 'OFF'}</b>"
                            )
                        except Exception:
                            await tg.send(
                                "<b>👛 Solana Wallet</b>\n"
                                f"Address: <code>{html.escape(cfg.wallet_public_address)}</code>\n"
                                "Balance check failed temporarily.\n"
                                "Live trading remains <b>OFF</b> 🔒"
                            )

                elif cmd == "/livestatus":
                    signer = scanner.live.signer_status()
                    summary = scanner.live.summary()
                    signer_text = (
                        "NOT INSTALLED"
                        if not signer["installed"]
                        else ("MATCHED ✅" if signer["matches"] else "MISMATCH / INVALID ❌")
                    )
                    armed = scanner.live.execution_ready()
                    await tg.send(
                        "<b>🔐 Live Trading Safety</b>\n"
                        f"Master switch (Railway): <b>{'ON' if cfg.live_trading_enabled else 'OFF'}</b>\n"
                        f"Signer: <b>{signer_text}</b>\n"
                        f"Telegram kill switch: <b>{'PAUSED' if summary['paused'] else 'ARMED'}</b>\n"
                        f"Overall: <b>{'LIVE ARMED ⚠️' if armed else 'SAFE / NOT TRADING 🔒'}</b>\n\n"
                        f"Buy score: <b>{cfg.live_buy_score}/100</b>\n"
                        f"Buy size: <b>{cfg.live_buy_sol:.3f} SOL</b>\n"
                        f"Max open positions: <b>{cfg.live_max_open_positions}</b>\n"
                        f"Daily spend cap: <b>{cfg.live_daily_spend_sol:.3f} SOL</b>\n"
                        f"Daily realized-loss stop: <b>{cfg.live_daily_loss_sol:.3f} SOL</b>\n"
                        f"Minimum SOL reserve: <b>{cfg.live_min_sol_reserve:.3f} SOL</b>\n"
                        f"Slippage ceiling: <b>{cfg.live_slippage_bps / 100:.2f}%</b>\n"
                        f"Today's spend: <b>{summary['daily_spend_sol']:.6f} SOL</b>\n"
                        f"Today's realized P/L: <b>{summary['daily_realized_pnl_sol']:+.6f} SOL</b>"
                    )

                elif cmd == "/livepositions":
                    rows = scanner.live.open_positions()
                    if not rows:
                        await tg.send("<b>💰 Live positions</b>\nNo open real positions.")
                    else:
                        lines = ["<b>💰 Open REAL positions</b>"]
                        for i, p in enumerate(rows, 1):
                            entry = float(p.get("entry_price_usd") or 0)
                            last = float(p.get("last_price_usd") or 0)
                            ret = ((last / entry) - 1) * 100 if entry and last else 0
                            initial = int(p.get("initial_token_raw") or 0)
                            remaining = int(p.get("remaining_token_raw") or 0)
                            rem = remaining / initial * 100 if initial else 0
                            lines.append(
                                f"\n{i}. <b>{html.escape(str(p.get('token_symbol') or '?'))}</b>\n"
                                f"Entry score: {int(p.get('entry_score') or 0)}/100 | "
                                f"Last score: {int(p.get('last_score') or 0)}/100\n"
                                f"Price return: <b>{ret:+.1f}%</b> | Remaining: <b>{rem:.0f}%</b>"
                            )
                        await tg.send("\n".join(lines))

                elif cmd == "/stoptrading":
                    scanner.live.set_paused(True)
                    await tg.send(
                        "🛑 <b>LIVE TRADING PAUSED</b>\n"
                        "No new real buys or automatic real sells will be submitted."
                    )

                elif cmd == "/starttrading":
                    confirmation = " ".join(text.split()[1:]).strip().upper()
                    if confirmation != "CONFIRM":
                        await tg.send(
                            "Live trading was NOT armed.\n\n"
                            "Send exactly: <code>/starttrading CONFIRM</code>"
                        )
                        continue

                    signer = scanner.live.signer_status()
                    if not cfg.live_trading_enabled:
                        await tg.send(
                            "🔒 Live trading remains OFF.\n"
                            "Railway variable <code>LIVE_TRADING_ENABLED</code> is not true."
                        )
                    elif not signer["installed"]:
                        await tg.send(
                            "🔒 Live trading remains OFF.\n"
                            "The Railway signing secret is not installed."
                        )
                    elif not signer["matches"]:
                        await tg.send(
                            "🚨 Live trading remains OFF.\n"
                            "The signing key does not match the configured wallet."
                        )
                    else:
                        try:
                            balance = await market.solana_wallet_balance(
                                cfg.wallet_public_address, cfg.solana_rpc_url
                            )
                            required = cfg.live_buy_sol + cfg.live_min_sol_reserve
                            if balance < required:
                                await tg.send(
                                    "🔒 Live trading remains OFF.\n"
                                    f"Wallet needs at least {required:.6f} SOL "
                                    "for one buy plus reserve."
                                )
                            else:
                                scanner.live.set_paused(False)
                                await tg.send(
                                    "⚠️ <b>LIVE TRADING ARMED</b>\n"
                                    "The bot can submit REAL Solana swaps when all scanner "
                                    "and risk rules pass.\n\n"
                                    "Send <code>/stoptrading</code> to pause immediately."
                                )
                        except Exception:
                            await tg.send(
                                "🔒 Live trading remains OFF because wallet balance "
                                "could not be verified."
                            )

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
    market = MarketProviders(cfg.birdeye_api_key, cfg.jupiter_api_key)
    tg = Telegram(cfg.telegram_bot_token, store, cfg.telegram_chat_id)
    scanner = Scanner(cfg, store, market, tg)

    print("Meme Prospect Telegram Bot starting")
    print(f"interval={cfg.scan_interval_seconds}s threshold={cfg.alert_score_threshold} chains={cfg.chains}")
    print(f"paper_trader={'ON' if cfg.paper_trade_enabled else 'OFF'} position=${cfg.paper_position_usd}")
    print(f"live_master={'ON' if cfg.live_trading_enabled else 'OFF'} live_paused={scanner.live.is_paused()}")

    if not cfg.birdeye_api_key:
        print("WARNING: BIRDEYE_API_KEY missing; fresh-launch coverage is limited.")
    if not tg.chat_id:
        print("Telegram chat not paired yet. Send /start to your bot after it starts.")

    try:
        await asyncio.gather(scanner.loop(), command_loop(tg, scanner, cfg, store, market))
    finally:
        await market.close()
        await tg.close()

if __name__ == "__main__":
    asyncio.run(main())
