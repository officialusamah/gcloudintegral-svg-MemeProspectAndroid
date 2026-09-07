from __future__ import annotations
import asyncio, time
from dataclasses import dataclass
from .config import Config
from .db import Store
from .models import Candidate, PairSnapshot, ScoreResult
from .providers import MarketProviders
from .scoring import score_token
from .telegram import Telegram, alert_message, paper_buy_message, paper_sell_message
from .live import LiveTrader

@dataclass
class ScanItem:
    snapshot: PairSnapshot
    result: ScoreResult

def acceleration_signal(s: PairSnapshot, previous: dict | None) -> tuple[bool, str]:
    if not previous:
        return False, ""
    prev_vol = float(previous.get("volume_m5") or 0)
    prev_buys = int(previous.get("buys_m5") or 0)
    vol_growth = ((float(s.volume_m5 or 0) - prev_vol) / prev_vol) * 100 if prev_vol >= 1000 else None
    buy_growth = ((int(s.buys_m5 or 0) - prev_buys) / prev_buys) * 100 if prev_buys >= 10 else None
    rapid = (
        (vol_growth is not None and vol_growth >= 50)
        or (buy_growth is not None and buy_growth >= 50)
    )
    if not rapid:
        return False, ""
    parts = []
    if vol_growth is not None:
        parts.append(f"5m volume {vol_growth:+.0f}%")
    if buy_growth is not None:
        parts.append(f"5m buys {buy_growth:+.0f}%")
    return True, " | ".join(parts)

class Scanner:
    def __init__(self, cfg: Config, store: Store, market: MarketProviders, telegram: Telegram):
        self.cfg = cfg
        self.store = store
        self.market = market
        self.telegram = telegram
        self.live = LiveTrader(cfg, store, market, telegram)
        self.lock = asyncio.Lock()
        self.last_scan_ts = 0
        self.last_count = 0
        self.fast_rescan_requested = False


    async def _fast_rescan_soon(self):
        await asyncio.sleep(1)
        try:
            await self.run_once(send_alerts=False)
            print("immediate post-exit rescan completed")
        except Exception as e:
            print("immediate rescan error:", repr(e))

    def _paper_buy_allowed(self, snap: PairSnapshot, result: ScoreResult) -> bool:
        if not self.cfg.paper_trade_enabled:
            return False
        if result.hard_block or result.score < self.cfg.paper_buy_score:
            return False
        if self.cfg.paper_solana_only and snap.chain != "solana":
            return False
        if not snap.price_usd or snap.price_usd <= 0:
            return False
        if snap.liquidity_usd < self.cfg.paper_min_liquidity_usd:
            return False
        if snap.market_cap <= 0 or snap.market_cap > self.cfg.paper_max_market_cap_usd:
            return False
        if snap.price_change_m5 > self.cfg.paper_max_5m_move_percent:
            return False
        if self.store.get_open_paper_position(snap.chain, snap.token_address):
            return False
        if self.store.open_paper_count() >= self.cfg.paper_max_open_positions:
            return False
        return True

    async def _paper_manage(self, snap: PairSnapshot, result: ScoreResult):
        p = self.store.get_open_paper_position(snap.chain, snap.token_address)

        # Existing position: update mark and check exit rules.
        if p:
            self.store.update_paper_mark(p["id"], snap.price_usd, result.score, snap.liquidity_usd)
            entry = float(p["entry_price"] or 0)
            if entry <= 0 or snap.price_usd <= 0:
                return

            ret = ((float(snap.price_usd) / entry) - 1.0) * 100
            entry_liq = float(p["entry_liquidity"] or 0)
            liq_drop_trigger = (
                entry_liq > 0
                and snap.liquidity_usd <= entry_liq * (1 - self.cfg.paper_liquidity_drop_percent / 100.0)
            )

            # Emergency exits have priority over profit taking.
            exit_reason = None
            if ret <= -self.cfg.paper_stop_loss_percent:
                exit_reason = f"STOP LOSS {ret:+.1f}%"
            elif liq_drop_trigger:
                exit_reason = f"LIQUIDITY DROP ≥{self.cfg.paper_liquidity_drop_percent:.0f}%"
            elif result.score < self.cfg.paper_exit_score:
                exit_reason = f"SCORE COLLAPSE {result.score}/100"

            if exit_reason:
                current = self.store.get_open_paper_position(snap.chain, snap.token_address)
                if current and float(current["remaining_percent"] or 0) > 0:
                    sold = self.store.paper_sell(
                        current["id"], snap.price_usd,
                        float(current["remaining_percent"]), exit_reason
                    )
                    if sold and self.telegram.chat_id:
                        await self.telegram.send(paper_sell_message(snap.token_symbol, sold))
                return

            # Take profit 2 first; if a coin jumps straight above +100%, both TP1 and TP2 fire.
            current = self.store.get_open_paper_position(snap.chain, snap.token_address)
            if current and ret >= self.cfg.paper_tp2_percent and not int(current["tp2_done"]):
                if not int(current["tp1_done"]):
                    sold = self.store.paper_sell(
                        current["id"], snap.price_usd, self.cfg.paper_tp1_sell_percent,
                        f"TAKE PROFIT 1 +{self.cfg.paper_tp1_percent:.0f}%"
                    )
                    self.store.mark_paper_tp(current["id"], 1)
                    if sold and float(sold.get("remaining_percent") or 0) <= 0:
                        self.fast_rescan_requested = True
                    if sold and self.telegram.chat_id:
                        await self.telegram.send(paper_sell_message(snap.token_symbol, sold))
                current = self.store.get_open_paper_position(snap.chain, snap.token_address)
                if current:
                    sold = self.store.paper_sell(
                        current["id"], snap.price_usd, self.cfg.paper_tp2_sell_percent,
                        f"TAKE PROFIT 2 +{self.cfg.paper_tp2_percent:.0f}%"
                    )
                    self.store.mark_paper_tp(current["id"], 2)
                    if sold and self.telegram.chat_id:
                        await self.telegram.send(paper_sell_message(snap.token_symbol, sold))
                return

            current = self.store.get_open_paper_position(snap.chain, snap.token_address)
            if current and ret >= self.cfg.paper_tp1_percent and not int(current["tp1_done"]):
                sold = self.store.paper_sell(
                    current["id"], snap.price_usd, self.cfg.paper_tp1_sell_percent,
                    f"TAKE PROFIT 1 +{self.cfg.paper_tp1_percent:.0f}%"
                )
                self.store.mark_paper_tp(current["id"], 1)
                if sold and float(sold.get("remaining_percent") or 0) <= 0:
                    self.fast_rescan_requested = True
                if sold and self.telegram.chat_id:
                    await self.telegram.send(paper_sell_message(snap.token_symbol, sold))
            return

        # No position yet: auto-paper-buy only when all filters pass.
        if self._paper_buy_allowed(snap, result):
            opened = self.store.open_paper_position(
                snap, result.score, self.cfg.paper_position_usd
            )
            if opened and self.telegram.chat_id:
                await self.telegram.send(
                    paper_buy_message(snap, result.score, self.cfg.paper_position_usd)
                )

    async def run_once(self, send_alerts: bool = True) -> list[ScanItem]:
        if self.lock.locked():
            return []

        async with self.lock:
            self.last_scan_ts = int(time.time())
            candidates = await self.market.discover(self.cfg.chains)

            # Keep tracking open paper/signal positions even if they fall out of the
            # "new listings" discovery feed.
            by_key = {(c.chain, c.address): c for c in candidates}
            if self.cfg.paper_trade_enabled:
                for p in self.store.open_paper_positions():
                    key = (str(p["chain"]), str(p["token_address"]))
                    by_key.setdefault(
                        key,
                        Candidate(chain=key[0], address=key[1], source="paper_position"),
                    )
            for p in self.live.open_positions():
                key = (str(p["chain"]), str(p["token_address"]))
                by_key.setdefault(
                    key,
                    Candidate(chain=key[0], address=key[1], source="live_position"),
                )
            candidates = list(by_key.values())
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
                alert_before = self.store.alert_state(snap.chain, snap.token_address)

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
                rapid, acceleration = acceleration_signal(snap, prev)

                self.store.save_snapshot(snap, result)
                results.append(ScanItem(snap, result))

                # Paper and live engines operate independently of alert cooldowns.
                await self._paper_manage(snap, result)
                live_signal_closed = await self.live.manage(snap, result)
                if live_signal_closed:
                    self.fast_rescan_requested = True

                previous_alert_score = int(alert_before["last_score"]) if alert_before else None
                upgraded_to_green = (
                    previous_alert_score is not None
                    and previous_alert_score < self.cfg.alert_score_threshold
                    and result.score >= self.cfg.alert_score_threshold
                )

                should_alert = (
                    send_alerts
                    and self.telegram.chat_id
                    and not result.hard_block
                    and result.score >= self.cfg.early_watch_threshold
                    and self.store.can_alert(
                        snap.chain, snap.token_address, result.score,
                        self.cfg.alert_cooldown_minutes,
                        strong_threshold=self.cfg.alert_score_threshold,
                        rapid_acceleration=rapid,
                    )
                )

                if should_alert:
                    age_min = (
                        (int(time.time() * 1000) - snap.pair_created_at_ms) / 60000
                        if snap.pair_created_at_ms else 0
                    )
                    if upgraded_to_green:
                        headline = "🟢🚀 PROSPECT UPGRADED"
                        signal_type = "UPGRADED_STRONG"
                    elif rapid and alert_before:
                        headline = "🚀 RAPID ACCELERATION"
                        signal_type = "RAPID_ACCELERATION"
                    elif result.score >= self.cfg.alert_score_threshold:
                        headline = None
                        signal_type = "STRONG_PROSPECT"
                    else:
                        headline = None
                        signal_type = "EARLY_WATCH"

                    try:
                        await self.telegram.send(
                            alert_message(
                                snap, result, age_min, headline=headline,
                                acceleration=acceleration if rapid else "",
                                previous_score=previous_alert_score,
                            )
                        )
                        self.store.mark_alert(snap.chain, snap.token_address, result.score)
                        self.store.record_signal(snap, result.score, signal_type)
                    except Exception as e:
                        print("Telegram alert failed:", e)

            ordered = sorted(results, key=lambda x: x.result.score, reverse=True)
            if self.paper_rescan_requested:
                self.fast_rescan_requested = False
                asyncio.create_task(self._paper_rescan_soon())
            return ordered

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
