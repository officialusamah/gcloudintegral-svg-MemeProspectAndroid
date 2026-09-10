from __future__ import annotations

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

    ue._launch_first_strict_10_60_installed = True
    print(
        "STRICT LAUNCH-FIRST ON — 10-60s priority; 60-180s Ultra Early; "
        "180-300s Confirmation; >300s BUY blocked. "
        "Strong Breakout BUY disabled. Mint/freeze safety + Jupiter "
        "BUY/reverse-SELL checks remain mandatory; manual approval unchanged.",
        flush=True,
    )
