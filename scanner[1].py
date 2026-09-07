from __future__ import annotations
import asyncio, time
from dataclasses import dataclass
from .config import Config
from .db import Store
from .models import PairSnapshot, ScoreResult
from .providers import MarketProviders
from .scoring import score_token
from .telegram import Telegram, alert_message

@dataclass
class ScanItem:
    snapshot: PairSnapshot
    result: ScoreResult

class Scanner:
    def __init__(self, cfg: Config, store: Store, market: MarketProviders, telegram: Telegram):
        self.cfg = cfg
        self.store = store
        self.market = market
        self.telegram = telegram
        self.lock = asyncio.Lock()
        self.last_scan_ts = 0
        self.last_count = 0

    async def run_once(self, send_alerts: bool = True) -> list[ScanItem]:
        if self.lock.locked():
            return []
        async with self.lock:
            self.last_scan_ts = int(time.time())
            candidates = await self.market.discover(self.cfg.chains)
            self.last_count = len(candidates)

            sem = asyncio.Semaphore(8)
            async def enrich(c):
                async with sem:
                    try:
                        return c, await self.market.pair_snapshot(c)
                    except Exception:
                        return c, None
            enriched = await asyncio.gather(*(enrich(c) for c in candidates))
            results: list[ScanItem] = []

            for candidate, snap in enriched:
                if not snap:
                    continue
                prev = self.store.previous_snapshot(snap.chain, snap.token_address)

                # Only spend security calls on candidates with a plausible market/liquidity profile.
                security = None
                if (
                    self.cfg.birdeye_api_key
                    and snap.liquidity_usd >= self.cfg.min_liquidity_usd
                    and (not snap.market_cap or snap.market_cap <= self.cfg.max_market_cap_usd)
                ):
                    security = await self.market.security(snap.chain, snap.token_address)

                result = score_token(
                    snap, prev, security,
                    min_liquidity=self.cfg.min_liquidity_usd,
                    min_mcap=self.cfg.min_market_cap_usd,
                    max_mcap=self.cfg.max_market_cap_usd,
                    max_age_minutes=self.cfg.max_pair_age_minutes,
                )
                self.store.save_snapshot(snap, result)
                results.append(ScanItem(snap, result))

                if (
                    send_alerts
                    and self.telegram.chat_id
                    and not result.hard_block
                    and result.score >= self.cfg.early_watch_threshold
                    and self.store.can_alert(
                        snap.chain, snap.token_address, result.score, self.cfg.alert_cooldown_minutes
                    )
                ):
                    age_min = (
                        (int(time.time() * 1000) - snap.pair_created_at_ms) / 60000
                        if snap.pair_created_at_ms else 0
                    )
                    try:
                        await self.telegram.send(alert_message(snap, result, age_min))
                        self.store.mark_alert(snap.chain, snap.token_address, result.score)
                    except Exception as e:
                        print("Telegram alert failed:", e)

            return sorted(results, key=lambda x: x.result.score, reverse=True)

    async def loop(self):
        while True:
            try:
                top = await self.run_once(send_alerts=True)
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                lead = f"{top[0].snapshot.token_symbol}:{top[0].result.score}" if top else "none"
                print(f"[{stamp}] candidates={self.last_count} strongest={lead}")
            except Exception as e:
                print("scan error:", repr(e))
            await asyncio.sleep(self.cfg.scan_interval_seconds)
