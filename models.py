from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class Candidate:
    chain: str
    address: str
    source: str

@dataclass
class PairSnapshot:
    chain: str
    token_address: str
    token_name: str
    token_symbol: str
    pair_address: str
    dex_id: str
    url: str
    market_cap: float
    fdv: float
    liquidity_usd: float
    pair_created_at_ms: int
    price_usd: float
    price_change_m5: float
    price_change_h1: float
    volume_m5: float
    volume_h1: float
    buys_m5: int
    sells_m5: int
    buys_h1: int
    sells_h1: int
    socials_count: int
    boosts_active: int
    source: str
    raw: dict[str, Any] = field(default_factory=dict)

@dataclass
class ScoreResult:
    score: int
    reasons: list[str]
    warnings: list[str]
    hard_block: bool = False
    security_checked: bool = False
