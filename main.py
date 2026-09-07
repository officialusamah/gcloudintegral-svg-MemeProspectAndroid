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
/livestatus — live signal + wallet-confirm status
/livepositions — open live-market signal positions
/starttrading CONFIRM — unavailable (wallet approval required)
/stoptrading — pause live-market signals
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
                        f"Take profit: +{cfg.paper_tp1_percent:.0f}% → sell {cfg.paper_tp1_sell_percent:.0f}%\n"
                        f"After full profit exit: immediate rescan ON\n"
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
                                f"Auto execution: <b>DISABLED 🔒</b>\n"
                                f"Wallet-confirm buttons: <b>ON ✅</b>\n"
                                f"Live signals: <b>{'PAUSED' if scanner.live.is_paused() else 'ON'}</b>\n"
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
                    summary = scanner.live.summary()
                    await tg.send(
                        "<b>🔐 Live Bot Status</b>\n"
                        f"Real-money auto execution: <b>DISABLED 🔒</b>\n"
                        f"Wallet-confirm swap buttons: <b>ON ✅</b>\n"
                        f"On-chain wallet confirmation: <b>{'ON ✅' if cfg.wallet_trade_tracking_enabled else 'OFF'}</b>\n"
                        f"Priority action alerts: <b>ON 🚨</b>\n"
                        f"Live-market signals: <b>{'PAUSED' if summary['paused'] else 'ON'}</b>\n"
                        f"Signal buy score: <b>{cfg.live_signal_buy_score}/100</b>\n"
                        f"Profit signal: <b>+{cfg.live_signal_take_profit_percent:.0f}% → SELL ALL</b>\n"
                        f"Stop signal: <b>-{cfg.live_signal_stop_loss_percent:.0f}% → EXIT ALL</b>\n"
                        f"Signal positions allowed: <b>{cfg.live_signal_max_open_positions}</b>\n"
                        f"Immediate rescan after exit: <b>ON</b>\n\n"
                        "ℹ️ BUY/SELL buttons open Jupiter in Solflare. "
                        "You review and approve every real transaction in your wallet."
                    )

                elif cmd == "/livepositions":
                    rows = scanner.live.open_positions()
                    if not rows:
                        await tg.send(
                            "<b>📡 Live wallet-tracked positions</b>\n"
                            "No open live-market positions."
                        )
                    else:
                        lines = ["<b>📡 Live wallet-tracked positions</b>"]
                        for i, p in enumerate(rows, 1):
                            confirmed = bool(int(p.get("wallet_buy_confirmed") or 0))
                            exiting = bool(int(p.get("sell_requested") or 0))
                            if exiting:
                                state = "🟠 WAITING FOR SELL CONFIRMATION"
                            elif confirmed:
                                state = "🟢 BUY CONFIRMED / MONITORING"
                            else:
                                state = "🟡 WAITING FOR BUY CONFIRMATION"

                            entry = float(
                                p.get("confirmed_entry_price_usd")
                                or p.get("entry_price_usd")
                                or 0
                            )
                            last = float(p.get("last_price_usd") or 0)
                            ret = ((last / entry) - 1) * 100 if entry and last and confirmed else 0
                            lines.append(
                                f"\n{i}. <b>{html.escape(str(p.get('token_symbol') or '?'))}</b>\n"
                                f"State: <b>{state}</b>\n"
                                f"Entry score: {int(p.get('entry_score') or 0)}/100 | "
                                f"Last score: {int(p.get('last_score') or 0)}/100\n"
                                + (
                                    f"Confirmed return: <b>{ret:+.1f}%</b> | "
                                    f"Target: <b>+{cfg.live_signal_take_profit_percent:.0f}%</b>"
                                    if confirmed else
                                    "Profit monitoring starts after the wallet BUY is confirmed."
                                )
                            )
                        await tg.send("\n".join(lines))

                elif cmd == "/stoptrading":
                    scanner.live.set_paused(True)
                    await tg.send(
                        "🛑 <b>LIVE-MARKET SIGNALS PAUSED</b>\n"
                        "The scanner keeps running, but manual BUY/SELL signals are paused."
                    )

                elif cmd == "/starttrading":
                    await tg.send(
                        "🔒 <b>Autonomous real-money trading is disabled.</b>\n\n"
                        "Wallet-confirm mode is active:\n"
                        f"• BUY button at {cfg.live_signal_buy_score}/100+\n"
                        f"• Planned size {cfg.live_buy_sol:.3f} SOL\n"
                        f"• SELL ALL button at +{cfg.live_signal_take_profit_percent:.0f}%\n"
                        "• Immediate fresh scan after the exit signal\n\n"
                        "You approve each swap inside Solflare. The bot then confirms the wallet change on-chain before tracking or closing the position."
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
