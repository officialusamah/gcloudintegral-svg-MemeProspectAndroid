from __future__ import annotations

import asyncio
import inspect
import os
import textwrap
import time

from . import ultra_early as ue
from .live import LiveTrader


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _age_seconds(snap) -> int:
    try:
        created_ms = int(getattr(snap, "pair_created_at_ms", 0) or 0)
        if created_ms <= 0:
            return 10**9
        return max(0, int(time.time() - created_ms / 1000))
    except Exception:
        return 10**9


def _watch_send_state(scanner):
    """Create one shared Telegram pacing state for creation-time WATCH alerts."""
    if not hasattr(scanner, "_ultra_watch_send_lock"):
        scanner._ultra_watch_send_lock = asyncio.Lock()
    if not hasattr(scanner, "_ultra_watch_chat_next_allowed"):
        scanner._ultra_watch_chat_next_allowed = {}
    if not hasattr(scanner, "_ultra_watch_global_next_allowed"):
        scanner._ultra_watch_global_next_allowed = 0.0
    return scanner._ultra_watch_send_lock


async def _send_watch_safely(
    scanner,
    chat_id: str,
    text: str,
    buttons,
    listed_ts: int,
    max_age: int,
) -> bool:
    """Pace WATCH messages so Telegram is not flooded with sendMessage requests."""
    lock = _watch_send_state(scanner)
    min_chat_interval = max(
        1.0,
        _float("ULTRA_WATCH_MIN_CHAT_INTERVAL_SECONDS", 1.25),
    )
    min_global_interval = max(
        0.04,
        _float("ULTRA_WATCH_MIN_GLOBAL_INTERVAL_SECONDS", 0.08),
    )

    async with lock:
        now = time.monotonic()
        chat_next = float(
            scanner._ultra_watch_chat_next_allowed.get(str(chat_id), 0.0) or 0.0
        )
        global_next = float(scanner._ultra_watch_global_next_allowed or 0.0)
        wait_for = max(0.0, chat_next - now, global_next - now)
        if wait_for > 0:
            await asyncio.sleep(wait_for)

        # Keep these alerts genuinely inside the configured creation window.
        age = max(0, int(time.time()) - int(listed_ts or time.time()))
        if age > max_age:
            return False

        try:
            await scanner.telegram.send(
                text,
                str(chat_id),
                buttons=buttons,
                urgent=True,
            )
        except Exception as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if status is not None and int(status) == 429:
                retry_after = 2.0
                try:
                    payload = response.json() if response is not None else {}
                    retry_after = float(
                        ((payload.get("parameters") or {}).get("retry_after"))
                        or retry_after
                    )
                except Exception:
                    pass
                cooldown = max(1.0, min(30.0, retry_after))
                scanner._ultra_watch_global_next_allowed = (
                    time.monotonic() + cooldown
                )
                scanner._ultra_watch_chat_next_allowed[str(chat_id)] = (
                    time.monotonic() + cooldown
                )
                print(
                    f"ULTRA WATCH Telegram 429 — cooldown {cooldown:.1f}s",
                    flush=True,
                )
                return False
            raise

        sent_at = time.monotonic()
        scanner._ultra_watch_global_next_allowed = sent_at + min_global_interval
        scanner._ultra_watch_chat_next_allowed[str(chat_id)] = (
            sent_at + min_chat_interval
        )
        return True


async def _send_immediate_watch_alert(scanner, address: str, listed_ts: int) -> None:
    """
    Send a 0-30s WATCH alert only after minimal early activity appears.

    This is deliberately NOT a BUY signal. It does not bypass mint/freeze,
    momentum, liquidity depth, Jupiter BUY/reverse-SELL, or manual approval gates.
    """
    enabled = str(
        os.getenv("ULTRA_WATCH_ALERT_ENABLED", "true") or ""
    ).strip().lower()
    if enabled not in {"1", "true", "yes", "y", "on"}:
        return

    max_age = max(1, _int("ULTRA_WATCH_MAX_AGE_SECONDS", 30))
    min_activity_usd = max(
        0.0,
        _float("ULTRA_WATCH_MIN_ACTIVITY_USD", 100.0),
    )
    now = int(time.time())
    age = max(0, now - int(listed_ts or now))
    if age > max_age:
        return

    # Poll the fast Pump.fun snapshot until either early volume OR conservative
    # real-liquidity depth reaches the user's threshold. The task exits at the
    # end of the 0-30s window so stale creation alerts are never sent.
    snap = None
    wait_seconds = max(
        0.5,
        min(
            float(max_age),
            _float("ULTRA_WATCH_DATA_WAIT_SECONDS", float(max_age)),
        ),
    )
    deadline = min(
        time.monotonic() + wait_seconds,
        time.monotonic() + max(0.0, float(max_age - age)),
    )

    volume = 0.0
    liquidity = 0.0
    while True:
        current_age = max(
            0,
            int(time.time()) - int(listed_ts or time.time()),
        )
        if current_age > max_age:
            return

        try:
            snap = await ue._pump_direct_snapshot(scanner, address, listed_ts)
        except Exception:
            snap = None

        if snap is not None:
            volume = float(getattr(snap, "volume_m5", 0) or 0)
            liquidity = float(getattr(snap, "liquidity_usd", 0) or 0)
            if volume >= min_activity_usd or liquidity >= min_activity_usd:
                break

        if time.monotonic() >= deadline:
            return
        await asyncio.sleep(0.25)

    age = max(0, int(time.time()) - int(listed_ts or time.time()))
    if age > max_age or snap is None:
        return

    name = str(
        getattr(snap, "token_name", "") or f"Pump.fun {address[:6]}"
    )
    symbol = str(getattr(snap, "token_symbol", "") or "NEW")
    market_cap = float(getattr(snap, "market_cap", 0) or 0)
    buys = int(getattr(snap, "buys_m5", 0) or 0)
    sells = int(getattr(snap, "sells_m5", 0) or 0)
    volume = float(getattr(snap, "volume_m5", 0) or 0)
    liquidity = float(getattr(snap, "liquidity_usd", 0) or 0)

    mcap_line = f"${market_cap:,.0f}" if market_cap > 0 else "calculating…"
    activity_line = f"{buys} / {sells}"
    volume_line = f"${volume:,.0f}" if volume > 0 else "collecting…"
    liquidity_line = (
        f"${liquidity:,.0f}" if liquidity > 0 else "collecting…"
    )

    try:
        jupiter_url = scanner.live._jupiter_url("SOL", address)
        buttons = [[{"text": "🪐 BUY ON JUPITER", "url": jupiter_url}]]
    except Exception:
        buttons = None

    message = (
        "🆕 <b>ULTRA EARLY TOKEN — JUST LAUNCHED</b>\n\n"
        f"🟣 <b>{name} ({symbol})</b>\n"
        f"Age: <b>{age}s</b>\n"
        "Launch source: <b>Pump.fun</b>\n"
        f"Market cap: <b>{mcap_line}</b>\n"
        f"Liquidity: <b>{liquidity_line}</b>\n"
        f"Buys / sells: <b>{activity_line}</b>\n"
        f"Early volume: <b>{volume_line}</b>\n"
        f"Contract: <code>{address}</code>\n\n"
        f"✅ Early filter: <b>${min_activity_usd:,.0f}+ volume OR liquidity</b>\n"
        "👀 Status: <b>WATCHING</b>\n"
        "⚠️ This is a creation alert, <b>not</b> BUY SETUP READY. "
        "Safety, momentum and Jupiter reverse-SELL checks may still be pending.\n\n"
        "If you want to enter manually before the bot confirms it, tap Jupiter "
        "and verify the route/token yourself."
    )

    sent = 0
    for user in scanner.accounts.trade_users():
        try:
            ok = await _send_watch_safely(
                scanner,
                str(user["chat_id"]),
                message,
                buttons,
                listed_ts,
                max_age,
            )
            if ok:
                sent += 1
        except Exception as exc:
            print(
                f"ULTRA WATCH alert error — {address[:6]}…{address[-4:]} — "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    if sent:
        print(
            f"ULTRA WATCH SENT — {address[:6]}…{address[-4:]} — "
            f"age {age}s — volume ${volume:,.0f} — liquidity ${liquidity:,.0f} — "
            f"alerts {sent}",
            flush=True,
        )


def install() -> None:
    # STRICT Launch-First policy:
    # 10-60 sec = Launch-First
    # 60-180 sec = Ultra Early
    # 180-300 sec = Confirmation
    # >300 sec = NO BUY SETUP READY from any ordinary route.
    if getattr(ue, "_launch_first_strict_10_60_installed", False):
        return

    ue.LAUNCH_FIRST_MIN_AGE_SECONDS = max(
        5, _int("LAUNCH_FIRST_MIN_AGE_SECONDS", 10)
    )
    ue.LAUNCH_FIRST_MAX_AGE_SECONDS = max(
        ue.LAUNCH_FIRST_MIN_AGE_SECONDS,
        _int("LAUNCH_FIRST_MAX_AGE_SECONDS", 60),
    )
    ue.LAUNCH_FIRST_MIN_MCAP_USD = max(
        1_000.0, _float("LAUNCH_FIRST_MIN_MCAP_USD", 5_000.0)
    )
    ue.LAUNCH_FIRST_MIN_TXNS = max(
        2, _int("LAUNCH_FIRST_MIN_TXNS", 4)
    )
    ue.LAUNCH_FIRST_MIN_BUY_RATIO = min(
        0.90, max(0.50, _float("LAUNCH_FIRST_MIN_BUY_RATIO", 0.55))
    )
    ue.LAUNCH_FIRST_MIN_VOLUME_USD = max(
        0.0, _float("LAUNCH_FIRST_MIN_VOLUME_USD", 500.0)
    )
    ue.LAUNCH_FIRST_MIN_M5_MOVE = _float(
        "LAUNCH_FIRST_MIN_5M_MOVE_PERCENT", -5.0
    )
    ue.LAUNCH_FIRST_MAX_M5_MOVE = max(
        5.0, _float("LAUNCH_FIRST_MAX_5M_MOVE_PERCENT", 60.0)
    )

    ue.ULTRA_MAX_AGE_SECONDS = min(
        int(getattr(ue, "ULTRA_MAX_AGE_SECONDS", 300)), 300
    )
    ue.MOONSHOT_MAX_AGE_SECONDS = min(
        int(getattr(ue, "MOONSHOT_MAX_AGE_SECONDS", 300)), 300
    )

    # Disable the old up-to-20-minute breakout BUY route.
    ue.STRONG_BREAKOUT_ENABLED = False

    # Faster first-minute retry pacing.
    ue.ULTRA_DEX_REQUEST_INTERVAL = min(
        float(getattr(ue, "ULTRA_DEX_REQUEST_INTERVAL", 1.0)), 0.5
    )
    ue.ULTRA_DEX_RETRY_SECONDS = min(
        float(getattr(ue, "ULTRA_DEX_RETRY_SECONDS", 2.0)), 0.5
    )

    source = textwrap.dedent(inspect.getsource(ue._handle_ultra_listing))

    old_phase = '''        confirmation_zone = age > ULTRA_CONFIRMATION_START_SECONDS
        required_buy_ratio = (
            ULTRA_CONFIRMATION_MIN_BUY_RATIO if confirmation_zone
            else ULTRA_MIN_BUY_RATIO
        )
        allowed_m5_move = (
            ULTRA_CONFIRMATION_MAX_M5_MOVE if confirmation_zone
            else ULTRA_MAX_M5_MOVE
        )

        checks = [
            (bool(snap.price_usd and snap.price_usd > 0), "price not ready"),
            depth_check,
            (snap.market_cap > 0, "market cap not ready"),
            (snap.market_cap <= ULTRA_MAX_MCAP, f"market cap ${snap.market_cap:,.0f} > ${ULTRA_MAX_MCAP:,.0f}"),
            (total >= ULTRA_MIN_TXNS, f"transactions {total} < {ULTRA_MIN_TXNS}"),
            (buy_ratio >= required_buy_ratio, f"buy ratio {buy_ratio*100:.0f}% < {required_buy_ratio*100:.0f}%"),
            (snap.volume_m5 >= ULTRA_MIN_VOLUME, f"volume ${snap.volume_m5:,.0f} < ${ULTRA_MIN_VOLUME:,.0f}"),
            (snap.price_change_m5 <= allowed_m5_move, f"5m move {snap.price_change_m5:+.1f}% > {allowed_m5_move:.0f}%"),
        ]
'''

    new_phase = '''        launch_first_wait = age < LAUNCH_FIRST_MIN_AGE_SECONDS
        launch_first_zone = (
            LAUNCH_FIRST_MIN_AGE_SECONDS
            <= age
            <= LAUNCH_FIRST_MAX_AGE_SECONDS
        )
        confirmation_zone = age > ULTRA_CONFIRMATION_START_SECONDS
        required_buy_ratio = (
            ULTRA_CONFIRMATION_MIN_BUY_RATIO if confirmation_zone
            else ULTRA_MIN_BUY_RATIO
        )
        allowed_m5_move = (
            ULTRA_CONFIRMATION_MAX_M5_MOVE if confirmation_zone
            else ULTRA_MAX_M5_MOVE
        )

        if age > 300:
            print(
                f"ULTRA REJECT — {short} — age {age}s — strict 5-minute BUY cap",
                flush=True,
            )
            return

        if launch_first_wait:
            checks = [
                (
                    False,
                    f"launch-first collecting data — age {age}s < "
                    f"{LAUNCH_FIRST_MIN_AGE_SECONDS}s",
                ),
            ]
        elif launch_first_zone:
            checks = [
                (bool(snap.price_usd and snap.price_usd > 0), "price not ready"),
                depth_check,
                (snap.market_cap > 0, "market cap not ready"),
                (
                    snap.market_cap >= LAUNCH_FIRST_MIN_MCAP_USD,
                    f"market cap ${snap.market_cap:,.0f} < "
                    f"${LAUNCH_FIRST_MIN_MCAP_USD:,.0f}",
                ),
                (
                    snap.market_cap <= ULTRA_MAX_MCAP,
                    f"market cap ${snap.market_cap:,.0f} > ${ULTRA_MAX_MCAP:,.0f}",
                ),
                (
                    total >= LAUNCH_FIRST_MIN_TXNS,
                    f"launch transactions {total} < {LAUNCH_FIRST_MIN_TXNS}",
                ),
                (
                    buy_ratio >= LAUNCH_FIRST_MIN_BUY_RATIO,
                    f"launch buy ratio {buy_ratio*100:.0f}% < "
                    f"{LAUNCH_FIRST_MIN_BUY_RATIO*100:.0f}%",
                ),
                (
                    snap.volume_m5 >= LAUNCH_FIRST_MIN_VOLUME_USD,
                    f"launch volume ${snap.volume_m5:,.0f} < "
                    f"${LAUNCH_FIRST_MIN_VOLUME_USD:,.0f}",
                ),
                (
                    snap.price_change_m5 >= LAUNCH_FIRST_MIN_M5_MOVE,
                    f"momentum collapsing: {snap.price_change_m5:+.1f}% < "
                    f"{LAUNCH_FIRST_MIN_M5_MOVE:+.0f}%",
                ),
                (
                    snap.price_change_m5 <= LAUNCH_FIRST_MAX_M5_MOVE,
                    f"launch already overextended: {snap.price_change_m5:+.1f}% > "
                    f"{LAUNCH_FIRST_MAX_M5_MOVE:.0f}%",
                ),
            ]
        else:
            checks = [
                (bool(snap.price_usd and snap.price_usd > 0), "price not ready"),
                depth_check,
                (snap.market_cap > 0, "market cap not ready"),
                (snap.market_cap <= ULTRA_MAX_MCAP, f"market cap ${snap.market_cap:,.0f} > ${ULTRA_MAX_MCAP:,.0f}"),
                (total >= ULTRA_MIN_TXNS, f"transactions {total} < {ULTRA_MIN_TXNS}"),
                (buy_ratio >= required_buy_ratio, f"buy ratio {buy_ratio*100:.0f}% < {required_buy_ratio*100:.0f}%"),
                (snap.volume_m5 >= ULTRA_MIN_VOLUME, f"volume ${snap.volume_m5:,.0f} < ${ULTRA_MIN_VOLUME:,.0f}"),
                (snap.price_change_m5 <= allowed_m5_move, f"5m move {snap.price_change_m5:+.1f}% > {allowed_m5_move:.0f}%"),
            ]
'''

    replacements = (
        (old_phase, new_phase, "10-60s launch-window checks"),
        ('        if not _action_buy_mcap_ok(snap, result):\n',
         '        if not launch_first_zone and not _action_buy_mcap_ok(snap, result):\n',
         "launch-first market-cap route"),
        ('        if result.score < required_score:\n',
         '        if not launch_first_zone and result.score < required_score:\n',
         "launch-first score bypass"),
        ('            await asyncio.sleep(dex_sleep)\n            continue\n',
         '            await asyncio.sleep(min(float(dex_sleep), 0.5) if age <= LAUNCH_FIRST_MAX_AGE_SECONDS else dex_sleep)\n            continue\n',
         "market-data retry pacing"),
        ('            await asyncio.sleep(1)\n            continue\n\n        authorities_safe = await _mint_authorities_safe(scanner, address)\n',
         '            await asyncio.sleep(0.5 if age <= LAUNCH_FIRST_MAX_AGE_SECONDS else 1)\n            continue\n\n        authorities_safe = await _mint_authorities_safe(scanner, address)\n',
         "launch check retry pacing"),
        ('        sent = 0\n        for user in scanner.accounts.trade_users():\n',
         '        sent = 0\n        launch_route_pending = False\n        for user in scanner.accounts.trade_users():\n',
         "Jupiter pending flag"),
        ('''            if not sellable:
                print(
                    f"ULTRA REJECT — {short} — age {age}s — {sell_reason}",
                    flush=True,
                )
                continue
''',
         '''            if not sellable:
                if launch_first_zone:
                    launch_route_pending = True
                    print(
                        f"ULTRA WAIT — {short} — age {age}s — {sell_reason}; retrying Jupiter",
                        flush=True,
                    )
                else:
                    print(
                        f"ULTRA REJECT — {short} — age {age}s — {sell_reason}",
                        flush=True,
                    )
                continue
''',
         "Jupiter retry handling"),
        ('''        if sent:
            # Record the first alert globally. The normal scanner may later alert
            # this token only if it crosses into the configured Strong Prospect tier.
            scanner.store.mark_alert("solana", address, result.score)
            print(f"ULTRA PASS — {short} — age {age}s — score {result.score}/100 — alerts {sent}", flush=True)
        else:
            print(f"ULTRA REJECT — {short} — age {age}s — no eligible trade users / slot unavailable", flush=True)
        return
''',
         '''        if sent:
            scanner.store.mark_alert("solana", address, result.score)
            print(
                f"LAUNCH-FIRST PASS — {short} — age {age}s — "
                f"score {result.score}/100 — alerts {sent}",
                flush=True,
            )
            return

        if launch_first_zone and launch_route_pending:
            await asyncio.sleep(0.5)
            continue

        print(
            f"ULTRA REJECT — {short} — age {age}s — no eligible trade users / slot unavailable",
            flush=True,
        )
        return
''',
         "Jupiter retry loop"),
    )

    for old, new, label in replacements:
        if old not in source:
            raise RuntimeError(
                f"Launch-First strict patch failed: {label} not found"
            )
        source = source.replace(old, new, 1)

    namespace = ue.__dict__
    exec(compile(source, ue.__file__, "exec"), namespace, namespace)

    # Hard-cap the NORMAL LiveTrader BUY route too.
    previous_candidate_ok = LiveTrader._candidate_ok

    def _strict_age_candidate_ok(self, snap, result) -> bool:
        age = _age_seconds(snap)
        if age > 300:
            return False
        return previous_candidate_ok(self, snap, result)

    LiveTrader._candidate_ok = _strict_age_candidate_ok

    # Creation-time WATCH layer. This wraps the already patched Ultra handler,
    # so Helius, PumpPortal and Birdeye detections all share the same immediate
    # Telegram notification. It does not open a live position or mark a BUY.
    previous_ultra_handler = ue._handle_ultra_listing

    async def _watch_then_handle(scanner, address: str, listed_ts: int):
        seen = getattr(scanner, "_ultra_watch_alerted", None)
        if seen is None:
            seen = set()
            scanner._ultra_watch_alerted = seen
        if address not in seen:
            seen.add(address)
            if len(seen) > 5000:
                for old_address in list(seen)[:1000]:
                    seen.discard(old_address)
            asyncio.create_task(
                _send_immediate_watch_alert(scanner, address, listed_ts)
            )
        return await previous_ultra_handler(scanner, address, listed_ts)

    ue._handle_ultra_listing = _watch_then_handle

    ue._launch_first_strict_10_60_installed = True
    watch_enabled = str(
        os.getenv("ULTRA_WATCH_ALERT_ENABLED", "true") or ""
    ).strip().lower() in {"1", "true", "yes", "y", "on"}
    watch_min = max(0.0, _float("ULTRA_WATCH_MIN_ACTIVITY_USD", 100.0))
    print(
        f"ULTRA WATCH FILTER READY — enabled={watch_enabled} — "
        f"0-30s alert requires ${watch_min:,.0f}+ early volume OR liquidity; "
        "Telegram WATCH sends are paced to reduce 429 errors. "
        "STRICT LAUNCH-FIRST ON — 10-60s priority; 60-180s Ultra Early; "
        "180-300s Confirmation; >300s BUY blocked. "
        "Strong Breakout BUY disabled. Mint/freeze safety + Jupiter "
        "BUY/reverse-SELL checks remain mandatory; manual approval unchanged.",
        flush=True,
    )
