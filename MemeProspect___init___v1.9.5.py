from __future__ import annotations

import os
import time
import html

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


# v1.9.3: include the token contract address (CA) in every BUY alert.
if not getattr(LiveTrader, "_buy_alert_ca_v193", False):
    async def _send_buy_with_ca(self, user: dict, snap, result):
        await self.telegram.send(
            f"🚨🚨 <b>ACTION REQUIRED — BUY SETUP READY</b> 🚨🚨\n\n"
            f"🟢 <b>{html.escape(snap.token_name)} ({html.escape(snap.token_symbol)})</b>\n"
            f"Prospect score: <b>{result.score}/100</b>\n"
            f"Signal price: <b>${snap.price_usd:.10f}</b>\n"
            f"Market cap: <b>${snap.market_cap:,.0f}</b>\n"
            f"Liquidity: <b>${snap.liquidity_usd:,.0f}</b>\n"
            f"CA: <code>{html.escape(snap.token_address)}</code>\n\n"
            f"💰 Planned buy: <b>{self.cfg.live_buy_sol:.3f} SOL</b>\n"
            f"🎯 Profit target: <b>+{self.cfg.live_signal_take_profit_percent:.0f}% → SELL ALL</b>\n"
            f"🛑 Risk exit: <b>-{self.cfg.live_signal_stop_loss_percent:.0f}% → EXIT ALL</b>\n\n"
            f"Tap <b>BUY NOW</b>, verify the coin and CA in Solflare/Jupiter, then approve.\n"
            "The bot waits for your public wallet to confirm the purchase before tracking P/L.",
            str(user["chat_id"]),
            buttons=self._buy_buttons(snap.token_address),
            urgent=True,
        )

    LiveTrader._send_buy = _send_buy_with_ca
    LiveTrader._buy_alert_ca_v193 = True
    print("BUY alert CA display ON.")


# v1.9.4: richer fast-action BUY alert. Keeps manual wallet approval.
if not getattr(LiveTrader, "_buy_alert_fast_v194", False):
    async def _send_buy_fast(self, user: dict, snap, result):
        age_min = (
            max(0.0, (int(time.time() * 1000) - int(snap.pair_created_at_ms)) / 60000.0)
            if snap.pair_created_at_ms else 0.0
        )
        await self.telegram.send(
            f"🚨🚨 <b>ACTION REQUIRED — BUY SETUP READY</b> 🚨🚨\n\n"
            f"🟢 <b>{html.escape(snap.token_name)} ({html.escape(snap.token_symbol)})</b>\n"
            f"Prospect score: <b>{result.score}/100</b>\n"
            f"Age: <b>{age_min:.1f} min</b>\n"
            f"5m move: <b>{snap.price_change_m5:+.1f}%</b>\n"
            f"Signal price: <b>${snap.price_usd:.10f}</b>\n"
            f"Market cap: <b>${snap.market_cap:,.0f}</b>\n"
            f"Liquidity: <b>${snap.liquidity_usd:,.0f}</b>\n"
            f"CA: <code>{html.escape(snap.token_address)}</code>\n\n"
            f"💰 Planned buy: <b>{self.cfg.live_buy_sol:.3f} SOL</b>\n"
            f"🎯 Profit target: <b>+{self.cfg.live_signal_take_profit_percent:.0f}% → SELL ALL</b>\n"
            f"🛑 Risk exit: <b>-{self.cfg.live_signal_stop_loss_percent:.0f}% → EXIT ALL</b>\n\n"
            "Tap <b>BUY NOW</b>, verify the CA and amount, then approve in Solflare/Jupiter.",
            str(user["chat_id"]),
            buttons=self._buy_buttons(snap.token_address),
            urgent=True,
        )
    LiveTrader._send_buy = _send_buy_fast
    LiveTrader._buy_alert_fast_v194 = True
    print("Fast-action BUY alerts ON.")


# v1.9.5: $1 net-profit target + ultra-early 1-3 minute route.
#
# This remains a MANUAL-APPROVAL system:
# - The bot never signs or submits swaps.
# - BUY/SELL still require wallet approval.
# - Public wallet data is used only to confirm the trade.
#
# Dollar target is an estimate because slippage and execution price can change.
if not getattr(LiveTrader, "_dollar_target_v195", False):
    _original_init = LiveTrader.__init__
    _previous_candidate_ok = LiveTrader._candidate_ok

    def _patched_init(self, cfg, store, market, telegram, accounts):
        _original_init(self, cfg, store, market, telegram, accounts)

        # Non-destructive DB migration for new P/L fields.
        migrations = (
            "ALTER TABLE user_live_positions ADD COLUMN target_net_profit_usd REAL",
            "ALTER TABLE user_live_positions ADD COLUMN estimated_entry_value_usd REAL",
            "ALTER TABLE user_live_positions ADD COLUMN target_return_pct REAL",
            "ALTER TABLE user_live_positions ADD COLUMN estimated_profit_usd REAL",
        )
        with self.connect() as c:
            for sql in migrations:
                try:
                    c.execute(sql)
                except Exception:
                    pass

    def _target_usd(self) -> float:
        return max(0.01, _env_float("LIVE_TARGET_NET_PROFIT_USD", 1.0))

    def _cost_pct(self) -> float:
        return max(0.0, _env_float("LIVE_ESTIMATED_ROUNDTRIP_COST_PERCENT", 2.0))

    def _fixed_cost_usd(self) -> float:
        return max(0.0, _env_float("LIVE_ESTIMATED_FIXED_COST_USD", 0.05))

    def _entry_value_usd(self, p: dict, wallet: dict) -> float:
        entry = float(
            p.get("confirmed_entry_price_usd")
            or p.get("signal_entry_price_usd")
            or 0
        )
        raw = int(p.get("confirmed_token_raw") or 0)
        decimals = int(wallet.get("decimals") or 0)
        if entry <= 0 or raw <= 0:
            return 0.0
        token_ui = raw / (10 ** decimals)
        return max(0.0, token_ui * entry)

    def _required_return_pct(self, entry_value_usd: float) -> float:
        if entry_value_usd <= 0:
            # Safe fallback if exact position value cannot be derived.
            return float(self.cfg.live_signal_take_profit_percent)
        gross_needed_usd = _target_usd(self) + _fixed_cost_usd(self)
        return max(
            0.1,
            (gross_needed_usd / entry_value_usd) * 100.0 + _cost_pct(self),
        )

    def _estimated_net_profit(self, entry_value_usd: float, ret_pct: float) -> float:
        if entry_value_usd <= 0:
            return 0.0
        gross = entry_value_usd * (float(ret_pct) / 100.0)
        est_cost = entry_value_usd * (_cost_pct(self) / 100.0) + _fixed_cost_usd(self)
        return gross - est_cost

    def _save_target(self, pid: int, entry_value: float, target_pct: float):
        with self.connect() as c:
            c.execute(
                """UPDATE user_live_positions
                   SET target_net_profit_usd=?,
                       estimated_entry_value_usd=?,
                       target_return_pct=?
                   WHERE id=? AND status='OPEN'""",
                (
                    float(_target_usd(self)),
                    float(entry_value or 0),
                    float(target_pct or 0),
                    int(pid),
                ),
            )

    def _save_profit(self, pid: int, profit_usd: float):
        with self.connect() as c:
            c.execute(
                """UPDATE user_live_positions
                   SET estimated_profit_usd=?
                   WHERE id=?""",
                (float(profit_usd), int(pid)),
            )

    def _ultra_early_ok(self, snap, result) -> bool:
        if not self.cfg.live_signal_enabled:
            return False
        if snap.chain != "solana":
            return False
        if result.hard_block:
            return False

        score_min = max(1, _env_int("LIVE_ULTRA_EARLY_MIN_SCORE", 60))
        if int(result.score) < score_min:
            return False

        if not snap.price_usd or snap.price_usd <= 0:
            return False
        if not snap.pair_created_at_ms:
            return False

        age_min = max(
            0.0,
            (int(time.time() * 1000) - int(snap.pair_created_at_ms)) / 60000.0,
        )
        max_age = max(1.0, _env_float("LIVE_ULTRA_EARLY_MAX_AGE_MINUTES", 3.0))
        if age_min > max_age:
            return False

        min_liq = max(
            float(self.cfg.live_min_liquidity_usd),
            _env_float("LIVE_ULTRA_EARLY_MIN_LIQUIDITY_USD", 30_000),
        )
        if float(snap.liquidity_usd or 0) < min_liq:
            return False

        mc = float(snap.market_cap or 0)
        min_mc = max(1.0, _env_float("LIVE_ULTRA_EARLY_MIN_MARKET_CAP_USD", 20_000))
        max_mc = min(
            float(self.cfg.live_max_market_cap_usd),
            _env_float("LIVE_ULTRA_EARLY_MAX_MARKET_CAP_USD", 500_000),
        )
        if mc < min_mc or mc > max_mc:
            return False

        move = float(snap.price_change_m5 or 0)
        min_move = _env_float("LIVE_ULTRA_EARLY_MIN_5M_MOVE_PERCENT", 0.0)
        max_move = _env_float("LIVE_ULTRA_EARLY_MAX_5M_MOVE_PERCENT", 35.0)
        if move < min_move or move > max_move:
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
            tx5 >= max(1, _env_int("LIVE_ULTRA_EARLY_MIN_TX5", 20))
            and buy_ratio >= max(1.0, _env_float("LIVE_ULTRA_EARLY_MIN_BUY_RATIO", 1.6))
            and vol_liq >= max(
                0.0,
                _env_float("LIVE_ULTRA_EARLY_MIN_VOLUME_LIQUIDITY_RATIO", 0.25),
            )
        )

    def _candidate_ok_v195(self, snap, result) -> bool:
        # Keep the existing normal 75+ path and adaptive fast-launch path.
        if _previous_candidate_ok(self, snap, result):
            return True

        # Additional strict route for 1-3 minute Early Watch candidates.
        return _ultra_early_ok(self, snap, result)

    async def _send_buy_v195(self, user: dict, snap, result):
        age_min = (
            max(
                0.0,
                (int(time.time() * 1000) - int(snap.pair_created_at_ms)) / 60000.0,
            )
            if snap.pair_created_at_ms else 0.0
        )
        ultra = (
            int(result.score) < int(self.cfg.live_signal_buy_score)
            and age_min <= _env_float("LIVE_ULTRA_EARLY_MAX_AGE_MINUTES", 3.0)
        )
        label = (
            "⚡ <b>ULTRA EARLY $1 SETUP</b>"
            if ultra
            else "🚨🚨 <b>ACTION REQUIRED — BUY SETUP READY</b> 🚨🚨"
        )

        await self.telegram.send(
            f"{label}\n\n"
            f"🟢 <b>{html.escape(snap.token_name)} ({html.escape(snap.token_symbol)})</b>\n"
            f"Prospect score: <b>{result.score}/100</b>\n"
            f"Age: <b>{age_min:.1f} min</b>\n"
            f"5m move: <b>{snap.price_change_m5:+.1f}%</b>\n"
            f"Signal price: <b>${snap.price_usd:.10f}</b>\n"
            f"Market cap: <b>${snap.market_cap:,.0f}</b>\n"
            f"Liquidity: <b>${snap.liquidity_usd:,.0f}</b>\n"
            f"CA: <code>{html.escape(snap.token_address)}</code>\n\n"
            f"💰 Planned buy: <b>{self.cfg.live_buy_sol:.3f} SOL</b>\n"
            f"🎯 Target: <b>about ${_target_usd(self):.2f} NET profit</b>\n"
            f"🛑 Risk exit: <b>-{self.cfg.live_signal_stop_loss_percent:.0f}%</b>\n\n"
            "After the wallet confirms the BUY, the bot calculates the exact "
            "percentage needed for the dollar target from the confirmed position size.\n"
            "Tap <b>BUY NOW</b>, verify the CA and amount, then approve in your wallet.",
            str(user["chat_id"]),
            buttons=self._buy_buttons(snap.token_address),
            urgent=True,
        )

    async def _send_buy_confirmed_v195(
        self,
        p: dict,
        snap,
        entry_price: float,
        signature: str,
    ):
        try:
            wallet = await self._wallet_balance(
                str(p["wallet_address"]),
                snap.token_address,
            )
        except Exception:
            wallet = {}

        entry_value = _entry_value_usd(self, p, wallet)
        target_pct = _required_return_pct(self, entry_value)
        if entry_value > 0:
            _save_target(self, int(p["id"]), entry_value, target_pct)

        sig_line = (
            f"\nTransaction: <code>{html.escape(signature[:18])}…</code>"
            if signature else ""
        )
        value_line = (
            f"\nEstimated position value: <b>${entry_value:.2f}</b>"
            if entry_value > 0 else ""
        )

        await self.telegram.send(
            f"✅ <b>BUY CONFIRMED ON-CHAIN</b>\n\n"
            f"<b>{html.escape(snap.token_symbol)}</b>\n"
            f"Tracked entry: <b>${entry_price:.10f}</b>{sig_line}"
            f"{value_line}\n"
            f"🎯 Estimated net target: <b>+${_target_usd(self):.2f}</b>\n"
            f"Required move now: <b>about +{target_pct:.1f}%</b>\n"
            f"🛑 Risk exit: <b>-{self.cfg.live_signal_stop_loss_percent:.0f}%</b>\n\n"
            "Monitoring started. The target includes a configurable allowance "
            "for estimated costs/slippage.",
            str(p["user_chat_id"]),
            urgent=True,
        )

    async def _send_sell_v195(
        self,
        p: dict,
        snap,
        ret: float,
        reason: str,
    ):
        entry_value = float(p.get("estimated_entry_value_usd") or 0)
        est_profit = _estimated_net_profit(self, entry_value, ret)
        target_hit = reason.startswith("NET PROFIT TARGET")

        headline = (
            "🎯🚨 $1 NET TARGET REACHED — SELL ALL NOW"
            if target_hit
            else "🛑🚨 EXIT TRIGGERED — SELL ALL NOW"
        )

        profit_line = (
            f"Estimated net P/L: <b>${est_profit:+.2f}</b>\n"
            if entry_value > 0 else ""
        )

        await self.telegram.send(
            f"<b>{headline}</b>\n\n"
            f"<b>{html.escape(snap.token_symbol)}</b>\n"
            f"Tracked return: <b>{ret:+.1f}%</b>\n"
            f"{profit_line}"
            f"Reason: <b>{html.escape(reason)}</b>\n\n"
            "Tap <b>SELL ALL NOW</b>, choose MAX, verify the quote and approve.\n"
            "The bot will wait for the public wallet to confirm the exit, "
            "then your slot becomes free for the next prospect.",
            str(p["user_chat_id"]),
            buttons=self._sell_buttons(snap.token_address),
            urgent=True,
        )

    async def _send_sell_confirmed_v195(self, p: dict, snap, ret: float):
        entry_value = float(p.get("estimated_entry_value_usd") or 0)
        est_profit = _estimated_net_profit(self, entry_value, ret)
        if entry_value > 0:
            _save_profit(self, int(p["id"]), est_profit)

        profit_line = (
            f"Estimated net P/L: <b>${est_profit:+.2f}</b>\n"
            if entry_value > 0 else ""
        )

        await self.telegram.send(
            f"✅ <b>SELL CONFIRMED ON-CHAIN</b>\n\n"
            f"<b>{html.escape(snap.token_symbol)}</b>\n"
            f"Tracked result: <b>{ret:+.1f}%</b>\n"
            f"{profit_line}"
            "Position closed. Your live slot is free and the scanner can "
            "immediately look for the next eligible prospect.",
            str(p["user_chat_id"]),
            urgent=True,
        )

    async def _manage_position_v195(self, p: dict, snap, result) -> bool:
        self._update_mark(p["id"], snap, result)

        try:
            wallet = await self._wallet_balance(
                str(p["wallet_address"]), snap.token_address
            )
        except Exception:
            return False

        current_raw = int(wallet.get("raw") or 0)
        baseline = int(p.get("baseline_token_raw") or 0)
        buy_confirmed = bool(int(p.get("wallet_buy_confirmed") or 0))
        sell_requested = bool(int(p.get("sell_requested") or 0))

        if not buy_confirmed:
            if current_raw > baseline:
                entry, sig = await self._entry_price(p, snap)
                self._confirm_buy(p["id"], entry, current_raw - baseline)

                p = self.get_open(
                    str(p["user_chat_id"]), snap.chain, snap.token_address
                ) or p

                entry_value = _entry_value_usd(self, p, wallet)
                target_pct = _required_return_pct(self, entry_value)
                if entry_value > 0:
                    _save_target(self, int(p["id"]), entry_value, target_pct)
                    p = self.get_open(
                        str(p["user_chat_id"]), snap.chain, snap.token_address
                    ) or p

                await _send_buy_confirmed_v195(self, p, snap, entry, sig)
                return False

            if (
                int(time.time()) - int(p.get("opened_ts") or 0)
                >= self.cfg.wallet_buy_confirm_timeout_seconds
            ):
                self._close(p["id"], "BUY SIGNAL EXPIRED / NOT CONFIRMED")
                await self._send_expired(p, snap)
                return True

            return False

        entry = float(
            p.get("confirmed_entry_price_usd")
            or p.get("signal_entry_price_usd")
            or 0
        )
        ret = (
            ((float(snap.price_usd) / entry) - 1.0) * 100
            if entry > 0 and snap.price_usd and snap.price_usd > 0
            else 0.0
        )

        entry_value = float(p.get("estimated_entry_value_usd") or 0)
        if entry_value <= 0:
            entry_value = _entry_value_usd(self, p, wallet)

        target_pct = float(p.get("target_return_pct") or 0)
        if target_pct <= 0:
            target_pct = _required_return_pct(self, entry_value)
            if entry_value > 0:
                _save_target(self, int(p["id"]), entry_value, target_pct)

        est_profit = _estimated_net_profit(self, entry_value, ret)

        # Public wallet proves an exit, including a manual one.
        if current_raw <= baseline:
            reason = (
                "WALLET SELL CONFIRMED"
                if sell_requested
                else "MANUAL WALLET EXIT DETECTED"
            )
            self._close(p["id"], reason, float(snap.price_usd or 0), ret)
            _save_profit(self, int(p["id"]), est_profit)
            await _send_sell_confirmed_v195(self, p, snap, ret)
            return True

        if sell_requested:
            return False

        reason = ""
        if ret >= target_pct:
            reason = (
                f"NET PROFIT TARGET ~${_target_usd(self):.2f} "
                f"(required move {target_pct:.1f}%)"
            )
        elif ret <= -self.cfg.live_signal_stop_loss_percent:
            reason = f"STOP SIGNAL -{self.cfg.live_signal_stop_loss_percent:.0f}%"
        elif result.hard_block or result.score < self.cfg.live_exit_score:
            reason = f"RISK / SCORE COLLAPSE {result.score}/100"

        if reason:
            self._request_sell(p["id"])
            p = self.get_open(
                str(p["user_chat_id"]), snap.chain, snap.token_address
            ) or p
            await _send_sell_v195(self, p, snap, ret, reason)

        return False

    LiveTrader.__init__ = _patched_init
    LiveTrader._candidate_ok = _candidate_ok_v195
    LiveTrader._send_buy = _send_buy_v195
    LiveTrader._send_buy_confirmed = _send_buy_confirmed_v195
    LiveTrader._send_sell = _send_sell_v195
    LiveTrader._send_sell_confirmed = _send_sell_confirmed_v195
    LiveTrader._manage_position = _manage_position_v195

    LiveTrader._dollar_target_v195 = True

    print(
        "v1.9.5 ON: ~$1 estimated net-profit target, strict ultra-early "
        "1-3 minute route, manual wallet approval preserved."
    )
