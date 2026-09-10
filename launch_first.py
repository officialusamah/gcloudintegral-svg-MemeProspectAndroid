from __future__ import annotations

import inspect
import textwrap

from . import ultra_early as ue


def install() -> None:
    # Speed-optimized Launch-First 0-60 seconds.
    if getattr(ue, "_launch_first_speed_installed", False):
        return

    # Faster fallback pacing during the launch window.
    ue.ULTRA_DEX_REQUEST_INTERVAL = 0.5
    ue.ULTRA_DEX_RETRY_SECONDS = 0.5

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
'''
    new_mcap = '''        if not launch_first_zone and not _action_buy_mcap_ok(snap, result):
'''

    old_score = '''        if result.score < required_score:
'''
    new_score = '''        if not launch_first_zone and result.score < required_score:
'''

    old_no_snap_sleep = '''            await asyncio.sleep(dex_sleep)
            continue
'''
    new_no_snap_sleep = '''            await asyncio.sleep(min(float(dex_sleep), 0.5) if age <= 60 else dex_sleep)
            continue
'''

    old_failed_sleep = '''            await asyncio.sleep(1)
            continue

        authorities_safe = await _mint_authorities_safe(scanner, address)
'''
    new_failed_sleep = '''            await asyncio.sleep(0.5 if launch_first_zone else 1)
            continue

        authorities_safe = await _mint_authorities_safe(scanner, address)
'''

    old_sent = '''        sent = 0
        for user in scanner.accounts.trade_users():
'''
    new_sent = '''        sent = 0
        launch_route_pending = False
        for user in scanner.accounts.trade_users():
'''

    old_sell_fail = '''            if not sellable:
                print(
                    f"ULTRA REJECT — {short} — age {age}s — {sell_reason}",
                    flush=True,
                )
                continue
'''
    new_sell_fail = '''            if not sellable:
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
'''

    old_after_sent = '''        if sent:
            # Record the first alert globally. The normal scanner may later alert
            # this token only if it crosses into the configured Strong Prospect tier.
            scanner.store.mark_alert("solana", address, result.score)
            print(f"ULTRA PASS — {short} — age {age}s — score {result.score}/100 — alerts {sent}", flush=True)
        else:
            print(f"ULTRA REJECT — {short} — age {age}s — no eligible trade users / slot unavailable", flush=True)
        return
'''
    new_after_sent = '''        if sent:
            # Record the first alert globally. The normal scanner may later alert
            # this token only if it crosses into the configured Strong Prospect tier.
            scanner.store.mark_alert("solana", address, result.score)
            print(f"ULTRA PASS — {short} — age {age}s — score {result.score}/100 — alerts {sent}", flush=True)
            return

        if launch_first_zone and launch_route_pending:
            await asyncio.sleep(0.5)
            continue

        print(f"ULTRA REJECT — {short} — age {age}s — no eligible trade users / slot unavailable", flush=True)
        return
'''

    replacements = (
        (old_phase, new_phase, "launch-window checks"),
        (old_mcap, new_mcap, "minimum market-cap bypass"),
        (old_score, new_score, "score bypass"),
        (old_no_snap_sleep, new_no_snap_sleep, "market-data retry pacing"),
        (old_failed_sleep, new_failed_sleep, "launch check retry pacing"),
        (old_sent, new_sent, "Jupiter pending flag"),
        (old_sell_fail, new_sell_fail, "Jupiter retry handling"),
        (old_after_sent, new_after_sent, "Jupiter retry loop"),
    )

    for old, new, label in replacements:
        if old not in source:
            raise RuntimeError(f"Launch-First speed patch failed: {label} not found")
        source = source.replace(old, new, 1)

    namespace = ue.__dict__
    exec(compile(source, ue.__file__, "exec"), namespace, namespace)

    ue._launch_first_speed_installed = True
    print(
        "LAUNCH-FIRST SPEED ON — 0-60s fast market-data retries + "
        "0.5s Jupiter rechecks; score/tx-count/buy-ratio/volume/normal min-MC "
        "do not delay first-minute alerts. Hard safety + BUY/reverse-SELL "
        "checks remain; manual approval unchanged.",
        flush=True,
    )
