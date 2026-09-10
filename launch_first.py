from __future__ import annotations

import inspect
import textwrap

from . import ultra_early as ue


def install() -> None:
    # Launch-First 0-60 seconds.
    if getattr(ue, "_launch_first_fast_installed", False):
        return

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

    new_phase = '''        launch_first_zone = age <= 60
        confirmation_zone = age > ULTRA_CONFIRMATION_START_SECONDS
        required_buy_ratio = (
            ULTRA_CONFIRMATION_MIN_BUY_RATIO if confirmation_zone
            else ULTRA_MIN_BUY_RATIO
        )
        allowed_m5_move = (
            ULTRA_CONFIRMATION_MAX_M5_MOVE if confirmation_zone
            else ULTRA_MAX_M5_MOVE
        )

        if launch_first_zone:
            checks = [
                (bool(snap.price_usd and snap.price_usd > 0), "price not ready"),
                depth_check,
                (snap.market_cap > 0, "market cap not ready"),
                (snap.market_cap <= ULTRA_MAX_MCAP, f"market cap ${snap.market_cap:,.0f} > ${ULTRA_MAX_MCAP:,.0f}"),
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

    old_mcap = '''        if not _action_buy_mcap_ok(snap, result):
            reason = (
                f"market cap ${snap.market_cap:,.0f} outside actionable zones "
                f"(${ACTION_BUY_MIN_MCAP:,.0f}-${ACTION_BUY_MAX_MCAP:,.0f} normal, "
                f"moonshot, or strong-breakout criteria not met)"
            )
            if reason != last_reason:
                print(f"ULTRA WAIT — {short} — age {age}s — {reason}", flush=True)
                last_reason = reason
            await asyncio.sleep(1)
            continue
'''

    new_mcap = '''        if not launch_first_zone and not _action_buy_mcap_ok(snap, result):
            reason = (
                f"market cap ${snap.market_cap:,.0f} outside actionable zones "
                f"(${ACTION_BUY_MIN_MCAP:,.0f}-${ACTION_BUY_MAX_MCAP:,.0f} normal, "
                f"moonshot, or strong-breakout criteria not met)"
            )
            if reason != last_reason:
                print(f"ULTRA WAIT — {short} — age {age}s — {reason}", flush=True)
                last_reason = reason
            await asyncio.sleep(1)
            continue
'''

    old_score = '''        required_score = (
            ULTRA_CONFIRMATION_MIN_SCORE if confirmation_zone
            else ULTRA_MIN_SCORE
        )
        if result.score < required_score:
            phase = "confirmation" if confirmation_zone else "ultra"
            reason = f"{phase} score {result.score}/100 < {required_score}"
            if reason != last_reason:
                print(f"ULTRA WAIT — {short} — age {age}s — {reason}", flush=True)
                last_reason = reason
            await asyncio.sleep(1)
            continue
'''

    new_score = '''        required_score = (
            ULTRA_CONFIRMATION_MIN_SCORE if confirmation_zone
            else ULTRA_MIN_SCORE
        )
        if not launch_first_zone and result.score < required_score:
            phase = "confirmation" if confirmation_zone else "ultra"
            reason = f"{phase} score {result.score}/100 < {required_score}"
            if reason != last_reason:
                print(f"ULTRA WAIT — {short} — age {age}s — {reason}", flush=True)
                last_reason = reason
            await asyncio.sleep(1)
            continue
'''

    for old, new, label in (
        (old_phase, new_phase, "phase/check block"),
        (old_mcap, new_mcap, "market-cap gate"),
        (old_score, new_score, "score gate"),
    ):
        if old not in source:
            raise RuntimeError(f"Launch-First patch failed: {label} not found")
        source = source.replace(old, new, 1)

    namespace = ue.__dict__
    exec(compile(source, ue.__file__, "exec"), namespace, namespace)

    ue._launch_first_fast_installed = True
    print(
        "LAUNCH-FIRST FAST ON — 0-60s: no wait for score/tx-count/"
        "buy-ratio/volume/normal min-MC; hard safety + liquidity + "
        "Jupiter BUY/reverse-SELL still required.",
        flush=True,
    )
