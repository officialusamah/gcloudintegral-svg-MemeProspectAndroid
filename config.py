from __future__ import annotations
from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default

def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default

def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}

@dataclass
class Config:
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    birdeye_api_key: str = os.getenv("BIRDEYE_API_KEY", "").strip()

    scan_interval_seconds: int = max(60, _int("SCAN_INTERVAL_SECONDS", 60))
    alert_score_threshold: int = min(100, max(1, _int("ALERT_SCORE_THRESHOLD", 85)))
    early_watch_threshold: int = min(100, max(1, _int("EARLY_WATCH_THRESHOLD", 60)))

    chains: tuple[str, ...] = tuple(
        x.strip().lower()
        for x in os.getenv("CHAINS", "solana,bsc,base,ethereum,robinhood").split(",")
        if x.strip()
    )

    min_liquidity_usd: float = _float("MIN_LIQUIDITY_USD", 5_000)
    min_market_cap_usd: float = _float("MIN_MARKET_CAP_USD", 25_000)
    max_market_cap_usd: float = _float("MAX_MARKET_CAP_USD", 30_000_000)
    max_pair_age_minutes: int = _int("MAX_PAIR_AGE_MINUTES", 360)
    alert_cooldown_minutes: int = _int("ALERT_COOLDOWN_MINUTES", 45)

    # Paper trader: simulation only. It never signs a transaction or touches a wallet.
    paper_trade_enabled: bool = _bool("PAPER_TRADE_ENABLED", True)
    paper_solana_only: bool = _bool("PAPER_SOLANA_ONLY", True)
    paper_buy_score: int = min(100, max(1, _int("PAPER_BUY_SCORE", 85)))
    paper_position_usd: float = max(1.0, _float("PAPER_POSITION_USD", 25))
    paper_max_open_positions: int = max(1, _int("PAPER_MAX_OPEN_POSITIONS", 3))
    paper_min_liquidity_usd: float = max(0.0, _float("PAPER_MIN_LIQUIDITY_USD", 20_000))
    paper_max_market_cap_usd: float = max(0.0, _float("PAPER_MAX_MARKET_CAP_USD", 1_000_000))
    paper_max_5m_move_percent: float = max(0.0, _float("PAPER_MAX_5M_MOVE_PERCENT", 60))

    paper_tp1_percent: float = max(1.0, _float("PAPER_TP1_PERCENT", 50))
    paper_tp1_sell_percent: float = min(100.0, max(1.0, _float("PAPER_TP1_SELL_PERCENT", 25)))
    paper_tp2_percent: float = max(1.0, _float("PAPER_TP2_PERCENT", 100))
    paper_tp2_sell_percent: float = min(100.0, max(1.0, _float("PAPER_TP2_SELL_PERCENT", 25)))
    paper_stop_loss_percent: float = max(1.0, _float("PAPER_STOP_LOSS_PERCENT", 25))
    paper_exit_score: int = min(100, max(0, _int("PAPER_EXIT_SCORE", 50)))
    paper_liquidity_drop_percent: float = min(
        100.0, max(1.0, _float("PAPER_LIQUIDITY_DROP_PERCENT", 40))
    )

    db_path: str = os.getenv("DB_PATH", "data/meme_prospect.db")
