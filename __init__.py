from __future__ import annotations

import os
import time

# v1.9.2 adaptive 5-minute live-entry filter.
#
# Normal rule: keep the configured LIVE_MAX_5M_MOVE_PERCENT (40% by default).
# Exception: a token no older than 15 minutes may reach up to 60% in 5m only
# when liquidity, turnover, transaction activity, and buy pressure are strong.
#
# This patch changes signal eligibility only. It never signs or submits trades.

from .live import LiveTrader


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


if not getattr(LiveTrader, "_adaptive_5m_v192", False):
    def _adaptive_candidate_ok(self, snap, result) -> bool:
        if not self.cfg.live_signal_enabled:
            return False
        if snap.chain != "solana":
            return False
        if result.hard_block or result.score < self.cfg.live_signal_buy_score:
            return False
        if not snap.price_usd or snap.price_usd <= 0:
            return False
        if snap.liquidity_usd < self.cfg.live_min_liquidity_usd:
            return False
        if snap.market_cap <= 0 or snap.market_cap > self.cfg.live_max_market_cap_usd:
            return False

        # Standard entry path.
        if snap.price_change_m5 <= self.cfg.live_max_5m_move_percent:
            return True

        # Adaptive fast-launch exception.
        if not snap.pair_created_at_ms:
            return False

        age_limit = max(1, _env_int("LIVE_FAST_LAUNCH_AGE_MINUTES", 15))
        fast_move_limit = max(
            float(self.cfg.live_max_5m_move_percent),
            _env_float("LIVE_FAST_LAUNCH_MAX_5M_MOVE_PERCENT", 60),
        )
        fast_min_liquidity = max(
            float(self.cfg.live_min_liquidity_usd),
            _env_float("LIVE_FAST_LAUNCH_MIN_LIQUIDITY_USD", 50_000),
        )
        min_vol_liq = max(
            0.0,
            _env_float("LIVE_FAST_LAUNCH_MIN_VOLUME_LIQUIDITY_RATIO", 0.5),
        )
        min_tx5 = max(1, _env_int("LIVE_FAST_LAUNCH_MIN_TX5", 30))
        min_buy_ratio = max(
            1.0,
            _env_float("LIVE_FAST_LAUNCH_MIN_BUY_RATIO", 1.5),
        )

        age_min = max(
            0.0,
            (int(time.time() * 1000) - int(snap.pair_created_at_ms)) / 60000.0,
        )
        if age_min > age_limit:
            return False
        if snap.price_change_m5 > fast_move_limit:
            return False
        if snap.liquidity_usd < fast_min_liquidity:
            return False

        buys = int(snap.buys_m5 or 0)
        sells = int(snap.sells_m5 or 0)
        tx5 = buys + sells
        buy_ratio = buys / max(sells, 1)
        vol_liq = (
            float(snap.volume_m5 or 0) / float(snap.liquidity_usd)
            if snap.liquidity_usd else 0.0
        )

        return (
            tx5 >= min_tx5
            and buy_ratio >= min_buy_ratio
            and vol_liq >= min_vol_liq
        )

    LiveTrader._candidate_ok = _adaptive_candidate_ok
    LiveTrader._adaptive_5m_v192 = True

    print(
        "Adaptive live-entry filter ON: normal 5m limit "
        "from config; fast-launch exception up to 60% for <=15m "
        "with strong liquidity/volume/buy pressure."
    )
