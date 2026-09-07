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

    # Read-only Solana wallet connection.
    wallet_public_address: str = os.getenv(
        "WALLET_PUBLIC_ADDRESS",
        "3Ac3HVfYT45CJb3mC9wUh5cSHvLxkm6BgUtG8s5guZ1e"
    ).strip()
    solana_rpc_url: str = os.getenv(
        "SOLANA_RPC_URL",
        "https://api.mainnet-beta.solana.com"
    ).strip()

    # LIVE TRADING HAS THREE LOCKS:
    # 1) Railway master switch, 2) Railway-only signer, 3) Telegram arming.
    # It is OFF by default.
    live_trading_enabled: bool = False  # hard safety lock: no autonomous real-money execution
    solana_private_key_b58: str = os.getenv("SOLANA_PRIVATE_KEY_B58", "").strip()
    jupiter_api_key: str = os.getenv("JUPITER_API_KEY", "").strip()

    # Live-market SIGNAL mode (manual execution only).
    live_signal_enabled: bool = _bool("LIVE_SIGNAL_ENABLED", True)
    live_signal_buy_score: int = min(100, max(1, _int("LIVE_SIGNAL_BUY_SCORE", 80)))
    live_signal_take_profit_percent: float = max(
        1.0, _float("LIVE_SIGNAL_TAKE_PROFIT_PERCENT", 25)
    )
    live_signal_stop_loss_percent: float = max(
        1.0, _float("LIVE_SIGNAL_STOP_LOSS_PERCENT", 25)
    )
    live_signal_max_open_positions: int = max(
        1, _int("LIVE_SIGNAL_MAX_OPEN_POSITIONS", 1)
    )

    # Conservative Solana live-trading limits.
    live_buy_score: int = min(100, max(1, _int("LIVE_BUY_SCORE", 90)))
    live_buy_sol: float = max(0.001, _float("LIVE_BUY_SOL", 0.02))
    live_max_open_positions: int = max(1, _int("LIVE_MAX_OPEN_POSITIONS", 2))
    live_daily_spend_sol: float = max(0.001, _float("LIVE_DAILY_SPEND_SOL", 0.05))
    live_daily_loss_sol: float = max(0.001, _float("LIVE_DAILY_LOSS_SOL", 0.03))
    live_min_sol_reserve: float = max(0.01, _float("LIVE_MIN_SOL_RESERVE", 0.05))
    live_min_liquidity_usd: float = max(0.0, _float("LIVE_MIN_LIQUIDITY_USD", 30_000))
    live_max_market_cap_usd: float = max(0.0, _float("LIVE_MAX_MARKET_CAP_USD", 750_000))
    live_max_5m_move_percent: float = max(0.0, _float("LIVE_MAX_5M_MOVE_PERCENT", 40))
    live_slippage_bps: int = min(1000, max(10, _int("LIVE_SLIPPAGE_BPS", 300)))

    live_tp1_percent: float = max(1.0, _float("LIVE_TP1_PERCENT", 50))
    live_tp1_sell_percent: float = min(100.0, max(1.0, _float("LIVE_TP1_SELL_PERCENT", 25)))
    live_tp2_percent: float = max(1.0, _float("LIVE_TP2_PERCENT", 100))
    live_tp2_sell_percent: float = min(100.0, max(1.0, _float("LIVE_TP2_SELL_PERCENT", 25)))
    live_stop_loss_percent: float = max(1.0, _float("LIVE_STOP_LOSS_PERCENT", 25))
    live_exit_score: int = min(100, max(0, _int("LIVE_EXIT_SCORE", 50)))
    live_liquidity_drop_percent: float = min(
        100.0, max(1.0, _float("LIVE_LIQUIDITY_DROP_PERCENT", 40))
    )

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
    paper_buy_score: int = min(100, max(1, _int("PAPER_BUY_SCORE", 80)))
    paper_position_usd: float = max(1.0, _float("PAPER_POSITION_USD", 25))
    paper_max_open_positions: int = max(1, _int("PAPER_MAX_OPEN_POSITIONS", 3))
    paper_min_liquidity_usd: float = max(0.0, _float("PAPER_MIN_LIQUIDITY_USD", 20_000))
    paper_max_market_cap_usd: float = max(0.0, _float("PAPER_MAX_MARKET_CAP_USD", 1_000_000))
    paper_max_5m_move_percent: float = max(0.0, _float("PAPER_MAX_5M_MOVE_PERCENT", 60))

    paper_tp1_percent: float = max(1.0, _float("PAPER_TP1_PERCENT", 25))
    paper_tp1_sell_percent: float = min(100.0, max(1.0, _float("PAPER_TP1_SELL_PERCENT", 100)))
    paper_tp2_percent: float = max(1.0, _float("PAPER_TP2_PERCENT", 100))
    paper_tp2_sell_percent: float = min(100.0, max(1.0, _float("PAPER_TP2_SELL_PERCENT", 25)))
    paper_stop_loss_percent: float = max(1.0, _float("PAPER_STOP_LOSS_PERCENT", 25))
    paper_exit_score: int = min(100, max(0, _int("PAPER_EXIT_SCORE", 50)))
    paper_liquidity_drop_percent: float = min(
        100.0, max(1.0, _float("PAPER_LIQUIDITY_DROP_PERCENT", 40))
    )

    db_path: str = os.getenv("DB_PATH", "data/meme_prospect.db")
