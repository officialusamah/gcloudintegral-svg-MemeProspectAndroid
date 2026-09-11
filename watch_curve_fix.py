from __future__ import annotations

import asyncio
import os
import time

from . import launch_first as lf
from . import pumpportal_launch as ppl
from . import ultra_early as ue


def _truthy(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _num(payload: dict, *keys: str) -> float:
    for key in keys:
        try:
            value = float(payload.get(key) or 0)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return 0.0


def _watch_meta(scanner):
    cache = getattr(scanner, "_pumpportal_watch_meta", None)
    if cache is None:
        cache = {}
        scanner._pumpportal_watch_meta = cache
    return cache


def _trade_users(scanner) -> list:
    try:
        return list(scanner.accounts.trade_users())
    except Exception:
        return []


def _diag_slot(scanner) -> bool:
    if not _truthy("ULTRA_WATCH_DIAGNOSTICS_ENABLED", True):
        return False
    limit = max(1, lf._int("ULTRA_WATCH_DIAG_SAMPLE_PER_MINUTE", 8))
    now = time.monotonic()
    state = getattr(scanner, "_ultra_watch_diag_state", None)
    if not isinstance(state, dict) or now - float(state.get("started", 0.0)) >= 60.0:
        state = {"started": now, "count": 0}
        scanner._ultra_watch_diag_state = state
    if int(state.get("count", 0)) >= limit:
        return False
    state["count"] = int(state.get("count", 0)) + 1
    return True


def _short(address: str) -> str:
    return address[:6] + "…" + address[-4:] if len(address) > 12 else address


def _cache_pumpportal_watch_payload(scanner, mint: str, payload: dict) -> None:
    """PumpPortal is launch/name/symbol metadata only for WATCH."""
    cache = _watch_meta(scanner)
    existing = cache.get(mint) or {}
    existing.update(
        {
            "name": str(
                payload.get("name")
                or payload.get("tokenName")
                or payload.get("token_name")
                or existing.get("name")
                or ""
            ).strip(),
            "symbol": str(
                payload.get("symbol")
                or payload.get("ticker")
                or payload.get("tokenSymbol")
                or payload.get("token_symbol")
                or existing.get("symbol")
                or ""
            ).strip(),
            "market_cap_sol": _num(
                payload,
                "marketCapSol",
                "market_cap_sol",
            )
            or float(existing.get("market_cap_sol") or 0),
            "captured_ts": int(time.time()),
        }
    )
    cache[mint] = existing

    if len(cache) > 5000:
        oldest = sorted(
            cache.items(),
            key=lambda kv: int((kv[1] or {}).get("captured_ts") or 0),
        )[:1000]
        for key, _ in oldest:
            cache.pop(key, None)


def _portal_identity(scanner, address: str) -> tuple[str, str]:
    meta = (_watch_meta(scanner).get(address) or {})
    return (
        str(meta.get("name") or "").strip(),
        str(meta.get("symbol") or "").strip(),
    )


def _fresh_send_state(scanner):
    if not hasattr(scanner, "_ultra_watch_fresh_locks"):
        scanner._ultra_watch_fresh_locks = {}
    if not hasattr(scanner, "_ultra_watch_fresh_next_allowed"):
        scanner._ultra_watch_fresh_next_allowed = {}
    return scanner._ultra_watch_fresh_locks, scanner._ultra_watch_fresh_next_allowed


async def _send_watch_fresh(
    scanner,
    chat_id: str,
    text: str,
    buttons,
    listed_ts: int,
    max_age: int,
) -> bool:
    """
    Fresh-only WATCH sender.

    It never joins a long FIFO backlog. If a subscriber's Telegram lane is busy,
    this token waits only briefly and is then skipped so newer WATCH candidates
    are not trapped behind stale messages.
    """
    locks, next_allowed = _fresh_send_state(scanner)
    chat_id = str(chat_id)
    lock = locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[chat_id] = lock

    min_chat_interval = max(
        1.0,
        lf._float("ULTRA_WATCH_MIN_CHAT_INTERVAL_SECONDS", 1.25),
    )
    max_queue_wait = max(
        0.05,
        min(
            1.0,
            lf._float("ULTRA_WATCH_MAX_QUEUE_WAIT_SECONDS", 0.35),
        ),
    )

    try:
        await asyncio.wait_for(lock.acquire(), timeout=max_queue_wait)
    except asyncio.TimeoutError:
        return False

    try:
        now_mono = time.monotonic()
        allowed_at = float(next_allowed.get(chat_id, 0.0) or 0.0)
        wait_for = max(0.0, allowed_at - now_mono)

        # Do not build a stale queue.
        if wait_for > max_queue_wait:
            return False

        if wait_for > 0:
            await asyncio.sleep(wait_for)

        age_now = max(0, int(time.time()) - int(listed_ts or time.time()))
        if age_now > max_age:
            return False

        try:
            ok = await scanner.telegram.send(
                text,
                chat_id,
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
                next_allowed[chat_id] = time.monotonic() + cooldown
                print(
                    f"ULTRA WATCH Telegram 429 — chat {chat_id} — "
                    f"cooldown {cooldown:.1f}s",
                    flush=True,
                )
                return False
            print(
                f"ULTRA WATCH Telegram send error — chat {chat_id} — "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return False

        if not ok:
            return False

        next_allowed[chat_id] = time.monotonic() + min_chat_interval
        return True
    finally:
        lock.release()


async def _fixed_watch_alert(scanner, address: str, listed_ts: int) -> None:
    if not _truthy("ULTRA_WATCH_ALERT_ENABLED", True):
        return

    max_age = max(1, lf._int("ULTRA_WATCH_MAX_AGE_SECONDS", 30))
    min_activity_usd = max(
        0.0,
        lf._float("ULTRA_WATCH_MIN_ACTIVITY_USD", 50.0),
    )

    now = int(time.time())
    starting_age = max(0, now - int(listed_ts or now))
    if starting_age > max_age:
        return

    deadline = time.monotonic() + max(0.0, float(max_age - starting_age))
    snap = None
    volume = 0.0
    liquidity = 0.0
    max_volume = 0.0
    max_liquidity = 0.0

    # Only real Pump.fun on-chain activity can satisfy the WATCH threshold.
    while True:
        age = max(0, int(time.time()) - int(listed_ts or time.time()))

        if age > max_age:
            if _diag_slot(scanner):
                users = _trade_users(scanner)
                print(
                    f"WATCH BELOW FILTER — {_short(address)} — age {age}s — "
                    f"max volume ${max_volume:,.0f} — "
                    f"max liquidity ${max_liquidity:,.0f} — "
                    f"threshold ${min_activity_usd:,.0f} — "
                    f"eligible_users {len(users)}",
                    flush=True,
                )
            return

        try:
            snap = await ue._pump_direct_snapshot(scanner, address, listed_ts)
        except Exception:
            snap = None

        if snap is not None:
            volume = float(getattr(snap, "volume_m5", 0) or 0)
            liquidity = float(getattr(snap, "liquidity_usd", 0) or 0)
            max_volume = max(max_volume, volume)
            max_liquidity = max(max_liquidity, liquidity)

            if volume >= min_activity_usd or liquidity >= min_activity_usd:
                break

        if time.monotonic() >= deadline:
            if _diag_slot(scanner):
                users = _trade_users(scanner)
                print(
                    f"WATCH BELOW FILTER — {_short(address)} — age {age}s — "
                    f"max volume ${max_volume:,.0f} — "
                    f"max liquidity ${max_liquidity:,.0f} — "
                    f"threshold ${min_activity_usd:,.0f} — "
                    f"eligible_users {len(users)}",
                    flush=True,
                )
            return

        await asyncio.sleep(0.5)

    age = max(0, int(time.time()) - int(listed_ts or time.time()))
    if age > max_age or snap is None:
        return

    users = _trade_users(scanner)

    print(
        f"WATCH QUALIFIED — {_short(address)} — age {age}s — "
        f"volume ${volume:,.0f} — liquidity ${liquidity:,.0f} — "
        f"source Pump.fun on-chain — eligible_users {len(users)}",
        flush=True,
    )

    portal_name, portal_symbol = _portal_identity(scanner, address)
    name = portal_name or str(
        getattr(snap, "token_name", "") or f"Pump.fun {address[:6]}"
    )
    symbol = portal_symbol or str(getattr(snap, "token_symbol", "") or "NEW")

    market_cap = float(getattr(snap, "market_cap", 0) or 0)
    buys = int(getattr(snap, "buys_m5", 0) or 0)
    sells = int(getattr(snap, "sells_m5", 0) or 0)

    mcap_line = f"${market_cap:,.0f}" if market_cap > 0 else "calculating…"
    activity_line = f"{buys} / {sells}" if (buys or sells) else "collecting…"
    volume_line = f"${volume:,.0f}" if volume > 0 else "collecting…"
    liquidity_line = f"${liquidity:,.0f}" if liquidity > 0 else "collecting…"

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
        "Data source: <b>Pump.fun on-chain</b>\n"
        f"Market cap: <b>{mcap_line}</b>\n"
        f"Liquidity: <b>{liquidity_line}</b>\n"
        f"Buys / sells: <b>{activity_line}</b>\n"
        f"Early volume: <b>{volume_line}</b>\n"
        f"Contract: <code>{address}</code>\n\n"
        f"✅ Early filter: <b>${min_activity_usd:,.0f}+ real on-chain "
        "volume OR liquidity</b>\n"
        "👀 Status: <b>WATCHING</b>\n"
        "⚠️ This is a creation alert, <b>not</b> BUY SETUP READY. "
        "Mint/freeze, momentum and Jupiter BUY/reverse-SELL checks still apply "
        "before any BUY setup.\n\n"
        "Manual wallet approval remains required."
    )

    # Send to eligible users concurrently. Each user has an independent,
    # fresh-only pacing lane, so one subscriber cannot block another.
    results = await asyncio.gather(
        *[
            _send_watch_fresh(
                scanner,
                str(user["chat_id"]),
                message,
                buttons,
                listed_ts,
                max_age,
            )
            for user in users
        ],
        return_exceptions=True,
    )
    sent = sum(1 for result in results if result is True)

    if sent:
        print(
            f"ULTRA WATCH SENT — {_short(address)} — age {age}s — "
            f"volume ${volume:,.0f} — liquidity ${liquidity:,.0f} — "
            f"source Pump.fun on-chain — alerts {sent}",
            flush=True,
        )
    else:
        print(
            f"WATCH SKIPPED BUSY — {_short(address)} — age {age}s — "
            f"qualified=yes — eligible_users {len(users)} — "
            "fresh-only sender refused stale queueing",
            flush=True,
        )


def install() -> None:
    if getattr(lf, "_watch_curve_fix_installed", False):
        return
    lf._watch_curve_fix_installed = True

    lf._send_immediate_watch_alert = _fixed_watch_alert

    original_cache = ppl._cache_launch_metadata

    def patched_cache(scanner, mint: str, payload: dict) -> None:
        original_cache(scanner, mint, payload)
        _cache_pumpportal_watch_payload(scanner, mint, payload)

    ppl._cache_launch_metadata = patched_cache

    diag_limit = max(1, lf._int("ULTRA_WATCH_DIAG_SAMPLE_PER_MINUTE", 8))
    threshold = max(
        0.0,
        lf._float("ULTRA_WATCH_MIN_ACTIVITY_USD", 50.0),
    )
    queue_wait = max(
        0.05,
        min(
            1.0,
            lf._float("ULTRA_WATCH_MAX_QUEUE_WAIT_SECONDS", 0.35),
        ),
    )
    print(
        "ULTRA WATCH FRESH-SEND FIX ON — "
        "Pump.fun on-chain activity qualification only; "
        "PumpPortal metadata only; "
        f"threshold ${threshold:,.0f}; "
        f"max queue wait {queue_wait:.2f}s; "
        "per-user Telegram lanes; stale WATCH alerts skipped instead of queued; "
        f"diagnostics sampled up to {diag_limit}/min below-filter; "
        "BUY safety/Jupiter/manual approval unchanged.",
        flush=True,
    )
