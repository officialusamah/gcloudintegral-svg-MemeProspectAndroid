from __future__ import annotations

import asyncio
import html
import os
import re

from .accounts import Accounts
from .config import Config
from .db import Store
from .providers import MarketProviders
from .scanner import Scanner
from .telegram import Telegram

HELP = """<b>Meme Prospect Pro</b>

<b>Your commands</b>
/start — register
/plans — subscription status
/setwallet ADDRESS — connect your PUBLIC Solana wallet
/wallet — wallet balance
/livestatus — your live-signal status
/livepositions — your open wallet-tracked position
/abandon — stop tracking the current live position without selling
/mytrades — your recent closed trades
/top — strongest recent candidates
/referral — your share/referral link
/starttrading — enable new BUY signals
/stoptrading — pause new BUY signals
/help — show this help

🔐 Never send your seed phrase or private key to this bot."""

ADMIN_HELP = """
<b>Owner/admin commands</b>
/admin — business dashboard
/users — recent users
/activate CHAT_ID DAYS [AMOUNT_NGN] — activate subscription
/deactivate CHAT_ID — disable subscription
/scan — scan immediately
/broadcast MESSAGE — message active subscribers
"""

BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def valid_wallet(address: str) -> bool:
    return bool(BASE58_RE.fullmatch(str(address or "").strip()))


def mask_wallet(address: str) -> str:
    address = str(address or "")
    return address if len(address) < 14 else f"{address[:6]}…{address[-6:]}"


def score_icon(score: int) -> str:
    if score >= 85:
        return "🟢"
    if score >= 60:
        return "🟡"
    return "🔴"


def format_top(rows):
    if not rows:
        return "No candidates stored yet."
    lines = ["<b>Strongest recent candidates</b>"]
    for i, r in enumerate(rows[:5], 1):
        score = int(r["score"] or 0)
        lines.append(
            f"{i}. {score_icon(score)} <b>{html.escape(str(r['token_symbol'] or '?'))}</b> — "
            f"{score}/100 | {html.escape(str(r.get('chain') or '?').upper())} | "
            f"MC ${float(r['market_cap'] or 0):,.0f} | "
            f"Liq ${float(r['liquidity_usd'] or 0):,.0f} | "
            f"5m {float(r.get('price_change_m5') or 0):+.1f}%"
        )
    return "\n".join(lines)


def subscription_text(accounts: Accounts, chat_id: str, cfg: Config) -> str:
    active = accounts.is_active(chat_id)
    days = accounts.subscription_days_left(chat_id)

    if accounts.is_admin(chat_id):
        plan = "OWNER / ADMIN"
        expiry = "Unlimited"
    elif active:
        plan = "PRO ✅"
        expiry = f"{days} day(s) remaining"
    else:
        plan = "INACTIVE"
        expiry = "No active subscription"

    price = (
        f"₦{cfg.subscription_price_ngn:,.0f} / {cfg.subscription_default_days} days"
        if cfg.subscription_price_ngn > 0
        else "Contact the owner for Pro activation"
    )

    return (
        f"<b>💎 Meme Prospect Pro</b>\n"
        f"Plan: <b>{plan}</b>\n"
        f"Access: <b>{expiry}</b>\n"
        f"Pro price: <b>{price}</b>\n\n"
        "Your wallet remains under your control. Every real swap requires your approval."
    )


async def command_loop(
    tg: Telegram,
    scanner: Scanner,
    cfg: Config,
    store: Store,
    market: MarketProviders,
    accounts: Accounts,
):
    while True:
        try:
            updates = await tg.updates()

            for u in updates:
                tg.offset = max(tg.offset, int(u.get("update_id", 0)) + 1)

                msg = u.get("message") or {}
                chat = msg.get("chat") or {}
                sender = msg.get("from") or {}

                chat_id = str(chat.get("id") or "")
                text = str(msg.get("text") or "").strip()

                if not chat_id or not text:
                    continue

                username = str(sender.get("username") or "")
                first_name = str(sender.get("first_name") or "")
                parts = text.split()
                cmd = parts[0].split("@")[0].lower()

                referral = ""
                if (
                    cmd == "/start"
                    and len(parts) > 1
                    and parts[1].lower().startswith("ref_")
                ):
                    referral = parts[1][4:].upper()

                accounts.register(
                    chat_id,
                    username,
                    first_name,
                    referral_code=referral,
                )
                tg.chat_id = accounts.admin_chat_id() or tg.chat_id

                if cmd == "/start":
                    role = (
                        "\n\n👑 <b>You are the bot owner/admin.</b>"
                        if accounts.is_admin(chat_id)
                        else ""
                    )
                    await tg.send(
                        f"✅ <b>Welcome to Meme Prospect Pro.</b>\n\n"
                        f"Your Chat ID: <code>{html.escape(chat_id)}</code>\n"
                        "Connect only your <b>PUBLIC Solana wallet</b> with /setwallet.\n"
                        "Never provide a seed phrase or private key."
                        f"{role}\n\n{HELP}"
                        + (ADMIN_HELP if accounts.is_admin(chat_id) else ""),
                        chat_id,
                    )
                    continue

                if cmd == "/help":
                    await tg.send(
                        HELP + (ADMIN_HELP if accounts.is_admin(chat_id) else ""),
                        chat_id,
                    )

                elif cmd == "/plans":
                    await tg.send(
                        subscription_text(accounts, chat_id, cfg),
                        chat_id,
                    )

                elif cmd == "/setwallet":
                    if len(parts) != 2:
                        await tg.send(
                            "Use:\n<code>/setwallet YOUR_PUBLIC_SOLANA_ADDRESS</code>\n\n"
                            "Do not send a private key or seed phrase.",
                            chat_id,
                        )
                        continue

                    address = parts[1].strip()

                    if not valid_wallet(address):
                        await tg.send(
                            "That does not look like a valid Solana public address.",
                            chat_id,
                        )
                        continue

                    accounts.set_wallet(chat_id, address)
                    await tg.send(
                        f"✅ <b>Public wallet connected.</b>\n"
                        f"<code>{html.escape(mask_wallet(address))}</code>\n\n"
                        "Read-only tracking only. The bot cannot move funds.",
                        chat_id,
                    )

                elif cmd == "/wallet":
                    user = accounts.get(chat_id) or {}
                    wallet = str(user.get("wallet_address") or "")

                    if not wallet:
                        await tg.send(
                            "No wallet connected.\n"
                            "Use <code>/setwallet YOUR_PUBLIC_SOLANA_ADDRESS</code>",
                            chat_id,
                        )
                    else:
                        try:
                            sol = await market.solana_wallet_balance(
                                wallet,
                                cfg.solana_rpc_url,
                            )
                            await tg.send(
                                f"<b>👛 Your Solana Wallet</b>\n"
                                f"Address: <code>{html.escape(mask_wallet(wallet))}</code>\n"
                                f"SOL balance: <b>{sol:.6f} SOL</b>\n"
                                "Auto execution: <b>DISABLED 🔒</b>\n"
                                "Wallet confirmation: <b>ON ✅</b>",
                                chat_id,
                            )
                        except Exception:
                            await tg.send(
                                f"<b>👛 Your Solana Wallet</b>\n"
                                f"Address: <code>{html.escape(mask_wallet(wallet))}</code>\n"
                                "Balance check failed temporarily.",
                                chat_id,
                            )

                elif cmd == "/livestatus":
                    user = accounts.get(chat_id) or {}
                    wallet = str(user.get("wallet_address") or "")
                    active = accounts.is_active(chat_id)
                    enabled = bool(int(user.get("live_enabled") or 0))

                    target_usd = max(
                        0.01,
                        _env_float("LIVE_TARGET_NET_PROFIT_USD", 1.0),
                    )
                    ultra_score = max(
                        1,
                        _env_int("LIVE_ULTRA_EARLY_MIN_SCORE", 60),
                    )
                    ultra_age = max(
                        1.0,
                        _env_float("LIVE_ULTRA_EARLY_MAX_AGE_MINUTES", 3.0),
                    )

                    await tg.send(
                        "<b>🔐 Your Live Bot Status</b>\n"
                        "Real-money auto execution: <b>DISABLED 🔒</b>\n"
                        f"Subscription: <b>{'ACTIVE ✅' if active else 'INACTIVE'}</b>\n"
                        f"Public wallet: <b>{'CONNECTED ✅' if wallet else 'NOT CONNECTED'}</b>\n"
                        f"New BUY signals: <b>{'ON ✅' if active and enabled and wallet else 'OFF'}</b>\n"
                        f"Normal BUY route: <b>{cfg.live_signal_buy_score}+/100</b>\n"
                        f"Ultra Early route: <b>ON ✅ — up to {ultra_age:.0f} min / {ultra_score}+ with strict checks</b>\n"
                        f"Profit target: <b>~${target_usd:.2f} estimated NET profit → SELL ALL</b>\n"
                        f"Stop signal: <b>-{cfg.live_signal_stop_loss_percent:.0f}% → EXIT ALL</b>\n"
                        f"Positions allowed: <b>{cfg.live_signal_max_open_positions}</b>\n"
                        "Next-position fast rescan: <b>ON ✅</b>\n"
                        "On-chain BUY/SELL confirmation: <b>ON ✅</b>",
                        chat_id,
                    )

                elif cmd == "/livepositions":
                    rows = scanner.live.open_positions(chat_id)

                    if not rows:
                        await tg.send(
                            "<b>📡 Your live positions</b>\nNo open position.",
                            chat_id,
                        )
                    else:
                        lines = ["<b>📡 Your live position</b>"]

                        for p in rows:
                            confirmed = bool(
                                int(p.get("wallet_buy_confirmed") or 0)
                            )
                            exiting = bool(
                                int(p.get("sell_requested") or 0)
                            )

                            state = (
                                "🟠 WAITING FOR SELL CONFIRMATION"
                                if exiting
                                else (
                                    "🟢 BUY CONFIRMED / MONITORING"
                                    if confirmed
                                    else "🟡 WAITING FOR BUY CONFIRMATION"
                                )
                            )

                            entry = float(
                                p.get("confirmed_entry_price_usd")
                                or p.get("signal_entry_price_usd")
                                or 0
                            )
                            last = float(p.get("last_price_usd") or 0)
                            ret = (
                                ((last / entry) - 1) * 100
                                if confirmed and entry and last
                                else 0
                            )

                            extra = ""
                            if confirmed:
                                entry_value = float(
                                    p.get("estimated_entry_value_usd") or 0
                                )
                                target_pct = float(
                                    p.get("target_return_pct") or 0
                                )
                                est_profit = float(
                                    p.get("estimated_profit_usd") or 0
                                )
                                if entry_value > 0:
                                    extra += (
                                        f"\nEstimated position: <b>${entry_value:.2f}</b>"
                                    )
                                if target_pct > 0:
                                    extra += (
                                        f"\nCurrent $1 target move: <b>+{target_pct:.1f}%</b>"
                                    )
                                if est_profit:
                                    extra += (
                                        f"\nStored estimated P/L: <b>${est_profit:+.2f}</b>"
                                    )

                            lines.append(
                                f"\n<b>{html.escape(str(p.get('token_symbol') or '?'))}</b>\n"
                                f"State: <b>{state}</b>\n"
                                f"Entry score: {int(p.get('entry_score') or 0)}/100\n"
                                + (
                                    f"Tracked return: <b>{ret:+.1f}%</b>{extra}"
                                    if confirmed
                                    else "P/L starts after on-chain BUY confirmation."
                                )
                            )

                        await tg.send("\n".join(lines), chat_id)

                elif cmd == "/abandon":
                    rows = scanner.live.open_positions(chat_id)

                    if not rows:
                        await tg.send(
                            "<b>📡 No open position to abandon.</b>\n"
                            "Your live BUY slot is already free.",
                            chat_id,
                        )

                    elif len(rows) > 1:
                        await tg.send(
                            "More than one open position exists, so /abandon "
                            "was not applied automatically.",
                            chat_id,
                        )

                    else:
                        p = rows[0]

                        confirmed = bool(
                            int(p.get("wallet_buy_confirmed") or 0)
                        )

                        entry = float(
                            p.get("confirmed_entry_price_usd")
                            or p.get("signal_entry_price_usd")
                            or 0
                        )

                        last = float(p.get("last_price_usd") or 0)

                        ret = (
                            ((last / entry) - 1) * 100
                            if confirmed and entry > 0 and last > 0
                            else None
                        )

                        scanner.live._close(
                            int(p["id"]),
                            "MANUALLY ABANDONED TRACKING",
                            last,
                            ret,
                        )

                        symbol = html.escape(
                            str(p.get("token_symbol") or "?")
                        )

                        await tg.send(
                            f"🧹 <b>Tracking abandoned — {symbol}</b>\n\n"
                            "The bot has stopped monitoring this position and "
                            "your live BUY slot is now free.\n\n"
                            "⚠️ <b>This does NOT sell the token.</b> "
                            "If you still hold it, it remains in your wallet.",
                            chat_id,
                            urgent=True,
                        )

                elif cmd == "/mytrades":
                    rows = scanner.live.closed_positions(chat_id, 10)

                    if not rows:
                        await tg.send(
                            "<b>📒 Your trade history</b>\n"
                            "No closed wallet-tracked trades yet.",
                            chat_id,
                        )
                    else:
                        lines = [
                            "<b>📒 Your recent wallet-tracked trades</b>"
                        ]

                        for p in rows:
                            ret = p.get("realized_return_pct")
                            ret_text = (
                                "n/a"
                                if ret is None
                                else f"{float(ret):+.1f}%"
                            )
                            profit = p.get("estimated_profit_usd")
                            profit_text = (
                                ""
                                if profit is None
                                else f" | est. ${float(profit):+.2f}"
                            )

                            lines.append(
                                f"\n<b>{html.escape(str(p.get('token_symbol') or '?'))}</b> — "
                                f"<b>{ret_text}</b>{profit_text}\n"
                                f"{html.escape(str(p.get('exit_reason') or 'Closed'))}"
                            )

                        await tg.send("\n".join(lines), chat_id)

                elif cmd == "/top":
                    if not accounts.is_active(chat_id):
                        await tg.send(
                            "💎 Pro access is inactive. Use /plans.",
                            chat_id,
                        )
                    else:
                        await tg.send(
                            format_top(store.latest_top(5)),
                            chat_id,
                        )

                elif cmd == "/starttrading":
                    user = accounts.get(chat_id) or {}

                    if not accounts.is_active(chat_id):
                        await tg.send(
                            "💎 Your Pro subscription is inactive. Use /plans.",
                            chat_id,
                        )

                    elif not str(user.get("wallet_address") or ""):
                        await tg.send(
                            "Connect your public Solana wallet first with /setwallet.",
                            chat_id,
                        )

                    else:
                        accounts.set_live_enabled(chat_id, True)
                        await tg.send(
                            "✅ <b>New BUY signals enabled.</b>\n"
                            "The bot can use the normal route and strict Ultra Early route. "
                            "Every real swap still requires your wallet approval.",
                            chat_id,
                        )

                elif cmd == "/stoptrading":
                    accounts.set_live_enabled(chat_id, False)
                    await tg.send(
                        "🛑 <b>New BUY signals paused.</b>\n"
                        "Any existing position will still be monitored for protective SELL alerts.",
                        chat_id,
                    )

                elif cmd == "/referral":
                    user = accounts.get(chat_id) or {}
                    code = str(user.get("referral_code") or "")
                    link = (
                        f"https://t.me/{cfg.bot_username}"
                        f"?start=ref_{code}"
                    )
                    count = accounts.referral_count(chat_id)

                    await tg.send(
                        f"<b>🤝 Your referral link</b>\n"
                        f"{html.escape(link)}\n\n"
                        f"Friends registered from your link: <b>{count}</b>",
                        chat_id,
                    )

                elif cmd == "/scan":
                    if not accounts.is_admin(chat_id):
                        continue

                    await tg.send(
                        "🔎 Running a scan now…",
                        chat_id,
                    )

                    top = await scanner.run_once(
                        send_alerts=True
                    )

                    await tg.send(
                        format_top(
                            [
                                {
                                    "score": x.result.score,
                                    "token_symbol": x.snapshot.token_symbol,
                                    "chain": x.snapshot.chain,
                                    "market_cap": x.snapshot.market_cap,
                                    "liquidity_usd": x.snapshot.liquidity_usd,
                                    "price_change_m5": x.snapshot.price_change_m5,
                                }
                                for x in top[:5]
                            ]
                        ),
                        chat_id,
                    )

                elif cmd == "/admin":
                    if not accounts.is_admin(chat_id):
                        continue

                    s = accounts.stats()

                    await tg.send(
                        "<b>👑 Meme Prospect Pro — Admin</b>\n"
                        f"Users: <b>{s['total_users']}</b>\n"
                        f"Active subscribers: <b>{s['active_users']}</b>\n"
                        f"Wallets connected: <b>{s['wallets']}</b>\n"
                        f"Recorded subscription revenue: <b>₦{s['recorded_revenue_ngn']:,.0f}</b>\n\n"
                        "Use /users to see accounts.",
                        chat_id,
                    )

                elif cmd == "/users":
                    if not accounts.is_admin(chat_id):
                        continue

                    rows = accounts.all_users(30)
                    lines = ["<b>👥 Users</b>"]

                    for urow in rows:
                        cid = str(urow["chat_id"])
                        name = str(
                            urow.get("username")
                            or urow.get("first_name")
                            or "user"
                        )
                        active = accounts.is_active(cid)
                        wallet_icon = (
                            "👛"
                            if str(urow.get("wallet_address") or "")
                            else "—"
                        )
                        role = (
                            "👑"
                            if int(urow.get("is_admin") or 0)
                            else ""
                        )

                        lines.append(
                            f"\n{role}<b>{html.escape(name)}</b> {wallet_icon}\n"
                            f"<code>{html.escape(cid)}</code> | "
                            f"{'ACTIVE' if active else 'INACTIVE'}"
                        )

                    await tg.send(
                        "\n".join(lines),
                        chat_id,
                    )

                elif cmd == "/activate":
                    if not accounts.is_admin(chat_id):
                        continue

                    if len(parts) < 3:
                        await tg.send(
                            "Use:\n"
                            "<code>/activate CHAT_ID DAYS [AMOUNT_NGN]</code>\n"
                            "Example: "
                            "<code>/activate 123456789 30 15000</code>",
                            chat_id,
                        )
                        continue

                    target = parts[1]

                    try:
                        days = int(parts[2])
                        amount = (
                            float(parts[3].replace(",", ""))
                            if len(parts) > 3
                            else 0
                        )
                    except ValueError:
                        await tg.send(
                            "Days and amount must be numbers.",
                            chat_id,
                        )
                        continue

                    accounts.activate(
                        target,
                        days,
                        amount,
                        "Manual admin activation",
                    )

                    await tg.send(
                        f"✅ Activated <code>{html.escape(target)}</code> "
                        f"for <b>{days} days</b>"
                        + (
                            f" — ₦{amount:,.0f} recorded"
                            if amount
                            else ""
                        ),
                        chat_id,
                    )

                    try:
                        await tg.send(
                            "✅ <b>Meme Prospect Pro activated.</b>\n"
                            f"Access: <b>{days} days</b>\n"
                            "Connect your public wallet with /setwallet "
                            "and use /starttrading.",
                            target,
                        )
                    except Exception:
                        pass

                elif cmd == "/deactivate":
                    if not accounts.is_admin(chat_id):
                        continue

                    if len(parts) != 2:
                        await tg.send(
                            "Use: <code>/deactivate CHAT_ID</code>",
                            chat_id,
                        )
                        continue

                    target = parts[1]
                    accounts.deactivate(target)

                    await tg.send(
                        f"🛑 Deactivated "
                        f"<code>{html.escape(target)}</code>.",
                        chat_id,
                    )

                elif cmd == "/broadcast":
                    if not accounts.is_admin(chat_id):
                        continue

                    body = text[len(parts[0]):].strip()

                    if not body:
                        await tg.send(
                            "Use: <code>/broadcast YOUR MESSAGE</code>",
                            chat_id,
                        )
                        continue

                    sent = 0

                    for target_user in accounts.active_users():
                        try:
                            await tg.send(
                                "📣 <b>Meme Prospect Pro</b>\n\n"
                                f"{html.escape(body)}",
                                str(target_user["chat_id"]),
                            )
                            sent += 1
                        except Exception:
                            pass

                    await tg.send(
                        f"✅ Broadcast sent to <b>{sent}</b> active users.",
                        chat_id,
                    )

        except Exception as e:
            print("telegram polling error:", repr(e))
            await asyncio.sleep(5)


async def main():
    cfg = Config()

    if not cfg.telegram_bot_token:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN.")

    store = Store(cfg.db_path)

    accounts = Accounts(
        cfg.db_path,
        cfg.admin_telegram_chat_id,
        cfg.wallet_public_address,
    )

    market = MarketProviders(
        cfg.birdeye_api_key,
        cfg.jupiter_api_key,
    )

    tg = Telegram(
        cfg.telegram_bot_token,
        store,
        accounts.admin_chat_id()
        or cfg.admin_telegram_chat_id,
    )

    scanner = Scanner(
        cfg,
        store,
        market,
        tg,
        accounts,
    )

    print("Meme Prospect Pro multi-user bot starting")
    print(
        f"interval={cfg.scan_interval_seconds}s "
        f"live_score={cfg.live_signal_buy_score} "
        f"active_users={len(accounts.active_users())}"
    )

    tasks = [
        asyncio.create_task(scanner.loop()),
        asyncio.create_task(
            command_loop(
                tg,
                scanner,
                cfg,
                store,
                market,
                accounts,
            )
        ),
    ]

    try:
        await asyncio.gather(*tasks)

    finally:
        for task in tasks:
            task.cancel()

        await market.close()
        await tg.close()


if __name__ == "__main__":
    asyncio.run(main())
