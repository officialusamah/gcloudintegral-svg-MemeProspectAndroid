from __future__ import annotations

import time

from . import ultra_early as ue


def install() -> None:
    """Install Launch-First 0-60 second priority without bypassing safety."""
    if getattr(ue, "_launch_first_060_installed", False):
        return

    original_score_token = ue.score_token
    launch_window_seconds = 60

    def launch_first_score_token(snap, *args, **kwargs):
        result = original_score_token(snap, *args, **kwargs)

        try:
            created_ms = int(getattr(snap, "pair_created_at_ms", 0) or 0)
            age_seconds = (
                max(0, int(time.time() - (created_ms / 1000)))
                if created_ms > 0
                else 10**9
            )
        except Exception:
            age_seconds = 10**9

        # During the first minute, do not let the normal score gate delay an
        # otherwise eligible launch. Existing activity, liquidity, mint/freeze,
        # hard-security, market-cap, Jupiter BUY and reverse-SELL checks remain.
        if age_seconds <= launch_window_seconds and not bool(
            getattr(result, "hard_block", False)
        ):
            minimum = int(getattr(ue, "ULTRA_MIN_SCORE", 65) or 65)
            if int(getattr(result, "score", 0) or 0) < minimum:
                result.score = minimum
                reasons = getattr(result, "reasons", None)
                if isinstance(reasons, list):
                    reasons.append(
                        "Launch-First 0-60s: score gate deferred; safety/tradability checks still required"
                    )

        return result

    ue.score_token = launch_first_score_token
    ue._launch_first_060_installed = True

    print(
        "LAUNCH-FIRST ON — 0-60s priority; 61-180s Ultra Early; "
        "181-300s Confirmation. Safety + Jupiter BUY/reverse-SELL checks unchanged.",
        flush=True,
    )
