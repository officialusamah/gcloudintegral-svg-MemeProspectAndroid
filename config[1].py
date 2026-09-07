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

@dataclass
class Config:
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    birdeye_api_key: str = os.getenv("BIRDEYE_API_KEY", "").strip()
    scan_interval_seconds: int = max(60, _int("SCAN_INTERVAL_SECONDS", 300))
    alert_score_threshold: int = min(100, max(1, _int("ALERT_SCORE_THRESHOLD", 85)))
    early_watch_threshold: int = min(100, max(1, _int("EARLY_WATCH_THRESHOLD", 60)))
    chains: tuple[str, ...] = tuple(
        x.strip().lower() for x in os.getenv("CHAINS", "solana,bsc,base,ethereum,robinhood").split(",") if x.strip()
    )
    min_liquidity_usd: float = _float("MIN_LIQUIDITY_USD", 5_000)
    min_market_cap_usd: float = _float("MIN_MARKET_CAP_USD", 25_000)
    max_market_cap_usd: float = _float("MAX_MARKET_CAP_USD", 30_000_000)
    max_pair_age_minutes: int = _int("MAX_PAIR_AGE_MINUTES", 360)
    alert_cooldown_minutes: int = _int("ALERT_COOLDOWN_MINUTES", 45)
    db_path: str = os.getenv("DB_PATH", "data/meme_prospect.db")
