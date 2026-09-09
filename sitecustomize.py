from __future__ import annotations

"""
Meme Prospect v1.9.6 - read-only realized P/L correction.

This patch NEVER signs or submits swaps. It only reads public Solana
transactions and corrects the final P/L after the wallet has already sold.

Upload this file to the REPOSITORY ROOT as:
    sitecustomize.py

Python loads sitecustomize automatically at startup, after which this patch
wraps the existing v1.9.5 live trader.
"""

import html
import time

try:
    from app.live import LiveTrader
    from app.providers import MarketProviders
except Exception:
    # Local/editor fallback. Railway uses the app.* imports above.
    LiveTrader = None
    MarketProviders = None


def _safe_float(value, default=0.0):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


if MarketProviders is not None and not getattr(MarketProviders, "_sell_fill_v196", False):

    async def solana_recent_sell_fill(
        self,
        owner: str,
        mint: str,
        since_ts: int,
        rpc_url: str = "https://api.mainnet-beta.solana.com",
        limit: int = 30,
    ) -> dict | None:
        """
        Read a recent confirmed transaction where the public wallet LOST
        this token and received SOL.

        This is read-only. It does not sign or submit anything.
        """
        if not owner or not mint:
            return None

        sig_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [
                owner,
                {
                    "limit": int(limit),
                    "commitment": "confirmed",
                },
            ],
        }

        r = await self.client.post(rpc_url, json=sig_payload)
        r.raise_for_status()
        sig_data = r.json()
        if sig_data.get("error"):
            raise RuntimeError(str(sig_data["error"]))

        for row in (sig_data.get("result") or []):
            if row.get("err") is not None:
                continue

            block_time = int(row.get("blockTime") or 0)
            if block_time and block_time < int(since_ts or 0) - 60:
                continue

            signature = str(row.get("signature") or "")
            if not signature:
                continue

            tx_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            }

            txr = await self.client.post(rpc_url, json=tx_payload)
            txr.raise_for_status()
            tx = (txr.json().get("result") or {})
            if not tx:
                continue

            meta = tx.get("meta") or {}
            if meta.get("err") is not None:
                continue

            msg = ((tx.get("transaction") or {}).get("message") or {})
            keys = msg.get("accountKeys") or []

            wallet_index = None
            for i, key in enumerate(keys):
                if self._account_key_text(key) == owner:
                    wallet_index = i
                    break
            if wallet_index is None:
                continue

            pre_raw, pre_dec = self._owned_token_raw(
                meta.get("preTokenBalances"), owner, mint
            )
            post_raw, post_dec = self._owned_token_raw(
                meta.get("postTokenBalances"), owner, mint
            )

            token_delta_raw = post_raw - pre_raw
            decimals = max(pre_dec, post_dec)

            # A SELL must reduce the user's token balance.
            if token_delta_raw >= 0:
                continue

            pre_bal = meta.get("preBalances") or []
            post_bal = meta.get("postBalances") or []
            if wallet_index >= len(pre_bal) or wallet_index >= len(post_bal):
                continue

            # post - pre already reflects the network fee paid by the wallet,
            # so this is the NET native-SOL change of the wallet in that tx.
            wallet_lamport_change = (
                int(post_bal[wallet_index]) - int(pre_bal[wallet_index])
            )

            if wallet_lamport_change <= 0:
                # Some routes can settle through WSOL in a way that makes the
                # native SOL delta unusable. In that case do not invent P/L.
                continue

            fee_lamports = int(meta.get("fee") or 0)
            token_sold_raw = abs(int(token_delta_raw))
            token_ui = (
                token_sold_raw / (10 ** decimals)
                if decimals >= 0 else 0.0
            )

            return {
                "signature": signature,
                "block_time": int(tx.get("blockTime") or block_time or 0),
                "token_sold_raw": token_sold_raw,
                "token_decimals": int(decimals),
                "token_ui": token_ui,
                "sol_received_net": wallet_lamport_change / 1_000_000_000,
                "network_fee_sol": fee_lamports / 1_000_000_000,
            }

        return None

    MarketProviders.solana_recent_sell_fill = solana_recent_sell_fill
    MarketProviders._sell_fill_v196 = True


if LiveTrader is not None and not getattr(LiveTrader, "_realized_pnl_v196", False):
    _previous_init_v196 = LiveTrader.__init__
    _previous_manage_v196 = LiveTrader._manage_position

    def _init_v196(self, cfg, store, market, telegram, accounts):
        _previous_init_v196(self, cfg, store, market, telegram, accounts)

        # Non-destructive DB migration.
        migrations = (
            "ALTER TABLE user_live_positions ADD COLUMN actual_exit_proceeds_usd REAL",
            "ALTER TABLE user_live_positions ADD COLUMN realized_profit_usd REAL",
            "ALTER TABLE user_live_positions ADD COLUMN sell_signature TEXT",
        )
        with self.connect() as c:
            for sql in migrations:
                try:
                    c.execute(sql)
                except Exception:
                    pass

    def _save_realized_v196(
        self,
        pid: int,
        proceeds_usd: float,
        profit_usd: float,
        signature: str,
    ):
        with self.connect() as c:
            c.execute(
                """UPDATE user_live_positions
                   SET actual_exit_proceeds_usd=?,
                       realized_profit_usd=?,
                       estimated_profit_usd=?,
                       sell_signature=?
                   WHERE id=?""",
                (
                    float(proceeds_usd),
                    float(profit_usd),
                    float(profit_usd),
                    str(signature or ""),
                    int(pid),
                ),
            )

    async def _send_realized_close_v196(
        self,
        p: dict,
        snap,
        realized_return_pct: float,
        realized_profit_usd: float,
        buy_cost_usd: float,
        proceeds_usd: float,
        signature: str,
    ):
        sig_line = (
            f"\nSell transaction: <code>{html.escape(signature[:18])}…</code>"
            if signature else ""
        )

        await self.telegram.send(
            f"✅ <b>SELL CONFIRMED ON-CHAIN</b>\n\n"
            f"<b>{html.escape(snap.token_symbol)}</b>\n"
            f"Realized result: <b>{realized_return_pct:+.1f}%</b>\n"
            f"Confirmed buy cost: <b>${buy_cost_usd:.2f}</b>\n"
            f"Confirmed sell proceeds: <b>${proceeds_usd:.2f}</b>\n"
            f"Realized P/L estimate: <b>${realized_profit_usd:+.2f}</b>"
            f"{sig_line}\n\n"
            "Position closed. Your live slot is free and the scanner can "
            "immediately look for the next eligible prospect.",
            str(p["user_chat_id"]),
            urgent=True,
        )

    async def _manage_position_v196(self, p: dict, snap, result) -> bool:
        """
        Intercept only a CONFIRMED wallet exit.

        All normal entry logic, $1 target logic, stop logic, Ultra Early logic,
        and manual wallet approval remain handled by v1.9.5.
        """
        buy_confirmed = bool(int(p.get("wallet_buy_confirmed") or 0))
        if not buy_confirmed:
            return await _previous_manage_v196(self, p, snap, result)

        try:
            wallet = await self._wallet_balance(
                str(p["wallet_address"]), snap.token_address
            )
        except Exception:
            return await _previous_manage_v196(self, p, snap, result)

        current_raw = int(wallet.get("raw") or 0)
        baseline = int(p.get("baseline_token_raw") or 0)

        # Position still exists: let v1.9.5 continue normal monitoring.
        if current_raw > baseline:
            return await _previous_manage_v196(self, p, snap, result)

        # Wallet balance proves the token position is gone.
        sell_requested = bool(int(p.get("sell_requested") or 0))
        reason = (
            "WALLET SELL CONFIRMED"
            if sell_requested
            else "MANUAL WALLET EXIT DETECTED"
        )

        buy_cost_usd = _safe_float(
            p.get("estimated_entry_value_usd"), 0.0
        )

        # Fallback buy-cost reconstruction when an older open row predates v1.9.5.
        if buy_cost_usd <= 0:
            entry = _safe_float(
                p.get("confirmed_entry_price_usd")
                or p.get("signal_entry_price_usd"),
                0.0,
            )
            raw = int(p.get("confirmed_token_raw") or 0)
            decimals = int(wallet.get("decimals") or 0)
            if entry > 0 and raw > 0:
                buy_cost_usd = (raw / (10 ** decimals)) * entry

        fill = None
        try:
            fill = await self.market.solana_recent_sell_fill(
                str(p["wallet_address"]),
                snap.token_address,
                int(
                    p.get("confirmed_entry_ts")
                    or p.get("opened_ts")
                    or 0
                ),
                self.cfg.solana_rpc_url,
            )
        except Exception:
            fill = None

        # If an exact sell transaction cannot be matched, fall back to the
        # existing v1.9.5 estimate rather than inventing a realized number.
        if not fill or buy_cost_usd <= 0:
            return await _previous_manage_v196(self, p, snap, result)

        sol_received = _safe_float(fill.get("sol_received_net"), 0.0)
        signature = str(fill.get("signature") or "")

        try:
            sol_usd = await self.market.solana_sol_price_usd()
        except Exception:
            sol_usd = 0.0

        if sol_received <= 0 or sol_usd <= 0:
            return await _previous_manage_v196(self, p, snap, result)

        proceeds_usd = sol_received * sol_usd
        realized_profit = proceeds_usd - buy_cost_usd
        realized_return = (
            (realized_profit / buy_cost_usd) * 100.0
            if buy_cost_usd > 0 else 0.0
        )

        # Close using the realized transaction-derived result.
        self._close(
            int(p["id"]),
            reason,
            float(snap.price_usd or 0),
            realized_return,
        )
        _save_realized_v196(
            self,
            int(p["id"]),
            proceeds_usd,
            realized_profit,
            signature,
        )

        await _send_realized_close_v196(
            self,
            p,
            snap,
            realized_return,
            realized_profit,
            buy_cost_usd,
            proceeds_usd,
            signature,
        )
        return True

    LiveTrader.__init__ = _init_v196
    LiveTrader._manage_position = _manage_position_v196
    LiveTrader._realized_pnl_v196 = True

    print(
        "v1.9.6 ON: transaction-derived realized P/L after confirmed wallet sell; "
        "manual wallet approval preserved."
    )
