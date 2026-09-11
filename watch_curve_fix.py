from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

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


def _cache_pumpportal_watch_payload(scanner, mint: str, payload: dict) -> None:
    """Keep the free PumpPortal create-event fields needed by the 0-30s WATCH layer."""
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
            "vsol": _num(
                payload,
                "vSolInBondingCurve",
                "virtualSolReserves",
                "virtual_sol_reserves",
            )
            or float(existing.get("vsol") or 0),
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


async def _pumpportal_creation_snapshot(scanner, address: str, listed_ts: int):
    """
    Build a WATCH-only snapshot from PumpPortal's free creation event.

    Pump.fun SOL curves start with a virtual SOL reserve. For SOL-paired coins,
    the increase above that starting reserve tracks real SOL accumulated in the
    curve. This is used only as the $100 WATCH-liquidity trigger; it does not
    bypass any BUY safety/Jupiter checks.
    """
    meta = (_watch_meta(scanner).get(address) or {}).copy()
    if not meta:
        return None

    vsol = float(meta.get("vsol") or 0)
    if vsol <= 0:
        return None

    initial_virtual_sol = max(
        1.0,
        lf._float("ULTRA_WATCH_INITIAL_VIRTUAL_SOL", 30.0),
    )
    real_sol_proxy = max(0.0, vsol - initial_virtual_sol)
    if real_sol_proxy <= 0:
        return None

    try:
        sol_usd = float(await ue._pump_sol_usd(scanner))
    except Exception:
        sol_usd = 0.0
    if sol_usd <= 0:
        return None

    liquidity_usd = real_sol_proxy * sol_usd
    market_cap_sol = float(meta.get("market_cap_sol") or 0)
    market_cap_usd = market_cap_sol * sol_usd if market_cap_sol > 0 else 0.0

    return SimpleNamespace(
        token_name=str(meta.get("name") or f"Pump.fun {address[:6]}"),
        token_symbol=str(meta.get("symbol") or "NEW"),
        market_cap=float(market_cap_usd),
        liquidity_usd=float(liquidity_usd),
        volume_m5=0.0,
        buys_m5=0,
        sells_m5=0,
        source="pumpportal_create",
        pair_created_at_ms=int(listed_ts or time.time()) * 1000,
    )


async def _fixed_watch_alert(scanner, address: str, listed_ts: int) -> None:
    if not _truthy("ULTRA_WATCH_ALERT_ENABLED", True):
        return

    max_age = max(1, lf._int("ULTRA_WATCH_MAX_AGE_SECONDS", 30))
    min_activity_usd = max(
        0.0,
        lf._float("ULTRA_WATCH_MIN_ACTIVITY_USD", 100.0),
    )

    now = int(time.time())
    starting_age = max(0, now - int(listed_ts or now))
    if starting_age > max_age:
        return

    # Always observe the full configured creation window. This deliberately
    # prevents an old Railway ULTRA_WATCH_DATA_WAIT_SECONDS=2 value from making
    # the WATCH task die before early activity appears.
    deadline = time.monotonic() + max(0.0, float(max_age - starting_age))
    snap = None
    source_label = ""
    volume = 0.0
    liquidity = 0.0

    while True:
        age = max(0, int(time.time()) - int(listed_ts or time.time()))
        if age > max_age:
            return

        # First choice: exact on-chain TradeEvent snapshot when available.
        try:
            snap = await ue._pump_direct_snapshot(scanner, address, listed_ts)
        except Exception:
            snap = None

        if snap is not None:
            volume = float(getattr(snap, "volume_m5", 0) or 0)
            liquidity = float(getattr(snap, "liquidity_usd", 0) or 0)
            source_label = "Pump.fun on-chain"
            if volume >= min_activity_usd or liquidity >= min_activity_usd:
                break

        # Fallback: PumpPortal's free create event already carries the current
        # virtual SOL reserve and market cap. This avoids waiting for the
        # TradeEvent log cache just to issue a WATCH alert.
        try:
            portal_snap = await _pumpportal_creation_snapshot(
                scanner, address, listed_ts
            )
        except Exception:
            portal_snap = None

        if portal_snap is not None:
            portal_liquidity = float(
                getattr(portal_snap, "liquidity_usd", 0) or 0
            )
            if portal_liquidity >= liquidity:
                snap = portal_snap
                volume = 0.0
                liquidity = portal_liquidity
                source_label = "PumpPortal launch data"
            if liquidity >= min_activity_usd:
                break

        if time.monotonic() >= deadline:
            return
        await asyncio.sleep(0.5)

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
    activity_line = f"{buys} / {sells}" if (buys or sells) else "collecting…"
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
        f"Data source: <b>{source_label or 'early market data'}</b>\n"
        f"Market cap: <b>{mcap_line}</b>\n"
        f"Liquidity: <b>{liquidity_line}</b>\n"
        f"Buys / sells: <b>{activity_line}</b>\n"
        f"Early volume: <b>{volume_line}</b>\n"
        f"Contract: <code>{address}</code>\n\n"
        f"✅ Early filter: <b>${min_activity_usd:,.0f}+ volume OR liquidity</b>\n"
        "👀 Status: <b>WATCHING</b>\n"
        "⚠️ This is a creation alert, <b>not</b> BUY SETUP READY. "
        "Mint/freeze, momentum and Jupiter BUY/reverse-SELL checks still apply "
        "before any BUY setup.\n\n"
        "Manual wallet approval remains required."
    )

    sent = 0
    for user in scanner.accounts.trade_users():
        try:
            ok = await lf._send_watch_safely(
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
            f"source {source_label or 'unknown'} — alerts {sent}",
            flush=True,
        )


def install() -> None:
    if getattr(lf, "_watch_curve_fix_installed", False):
        return
    lf._watch_curve_fix_installed = True

    # The already-installed Launch-First wrapper resolves this module global at
    # runtime, so replacing the function here fixes WATCH without touching BUY.
    lf._send_immediate_watch_alert = _fixed_watch_alert

    original_cache = ppl._cache_launch_metadata

    def patched_cache(scanner, mint: str, payload: dict) -> None:
        original_cache(scanner, mint, payload)
        _cache_pumpportal_watch_payload(scanner, mint, payload)

    ppl._cache_launch_metadata = patched_cache

    print(
        "ULTRA WATCH DATA FIX ON — full 0-30s wait; Pump.fun TradeEvent primary; "
        "PumpPortal create-event liquidity fallback enabled; $100 filter unchanged; "
        "BUY safety/Jupiter/manual approval unchanged.",
        flush=True,
    )
