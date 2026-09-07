from __future__ import annotations
import html, sqlite3, time
from urllib.parse import quote

SIGNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_signal_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chain TEXT NOT NULL,
  token_address TEXT NOT NULL,
  token_symbol TEXT,
  opened_ts INTEGER NOT NULL,
  closed_ts INTEGER,
  entry_price_usd REAL NOT NULL,
  entry_score INTEGER NOT NULL,
  entry_liquidity REAL,
  entry_market_cap REAL,
  last_price_usd REAL,
  last_score INTEGER,
  status TEXT NOT NULL DEFAULT 'OPEN',
  exit_reason TEXT,
  UNIQUE(chain, token_address, status)
);
CREATE INDEX IF NOT EXISTS idx_live_signal_positions_status
ON live_signal_positions(status, opened_ts DESC);
"""

class LiveTrader:
    """
    Live-market signal engine.

    IMPORTANT:
    This class intentionally does NOT sign, submit, buy, sell, or move funds.
    It watches real market data and sends manual BUY / SELL ALL signals only.
    """

    def __init__(self, cfg, store, market, telegram):
        self.cfg = cfg
        self.store = store
        self.market = market
        self.telegram = telegram
        with self.connect() as c:
            c.executescript(SIGNAL_SCHEMA)
            self._migrate(c)

    def connect(self):
        return sqlite3.connect(self.store.path)

    def _migrate(self, c):
        existing = {
            row[1] for row in c.execute("PRAGMA table_info(live_signal_positions)").fetchall()
        }
        wanted = {
            "baseline_token_raw": "INTEGER NOT NULL DEFAULT 0",
            "wallet_buy_confirmed": "INTEGER NOT NULL DEFAULT 0",
            "confirmed_entry_price_usd": "REAL",
            "confirmed_entry_ts": "INTEGER",
            "confirmed_token_raw": "INTEGER NOT NULL DEFAULT 0",
            "sell_requested": "INTEGER NOT NULL DEFAULT 0",
            "sell_signal_ts": "INTEGER",
            "wallet_sell_confirmed": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, ddl in wanted.items():
            if name not in existing:
                c.execute(f"ALTER TABLE live_signal_positions ADD COLUMN {name} {ddl}")
        c.commit()

    def is_paused(self) -> bool:
        return self.store.get_setting("live_signal_paused", "0") != "0"

    def set_paused(self, paused: bool):
        self.store.set_setting("live_signal_paused", "1" if paused else "0")

    def signer_status(self) -> dict:
        # Signer status is shown only for transparency; signal mode never uses it.
        return self.market.signer_status(
            self.cfg.solana_private_key_b58,
            self.cfg.wallet_public_address,
        )

    def execution_ready(self) -> bool:
        # Hard safety lock. Autonomous real-money execution is intentionally disabled.
        return False

    def open_positions(self):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                """SELECT * FROM live_signal_positions
                   WHERE status='OPEN' ORDER BY opened_ts DESC"""
            ).fetchall()
            return [dict(x) for x in rows]

    def open_count(self) -> int:
        with self.connect() as c:
            return int(c.execute(
                "SELECT COUNT(*) FROM live_signal_positions WHERE status='OPEN'"
            ).fetchone()[0] or 0)

    def get_open(self, chain: str, address: str):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                """SELECT * FROM live_signal_positions
                   WHERE chain=? AND token_address=? AND status='OPEN'
                   LIMIT 1""",
                (chain, address),
            ).fetchone()
            return dict(row) if row else None

    def summary(self) -> dict:
        return {
            "open": self.open_positions(),
            "paused": self.is_paused(),
        }

    def _signal_buy_allowed(self, snap, result) -> bool:
        if not self.cfg.live_signal_enabled or self.is_paused():
            return False
        if snap.chain != "solana":
            return False
        if result.hard_block or result.score < self.cfg.live_signal_buy_score:
            return False
        if not snap.price_usd or snap.price_usd <= 0:
            return False

        # Keep the same quality filters already used for live screening.
        if snap.liquidity_usd < self.cfg.live_min_liquidity_usd:
            return False
        if snap.market_cap <= 0 or snap.market_cap > self.cfg.live_max_market_cap_usd:
            return False
        if snap.price_change_m5 > self.cfg.live_max_5m_move_percent:
            return False

        if self.get_open(snap.chain, snap.token_address):
            return False
        if self._has_signal_history(snap.chain, snap.token_address):
            return False
        if self.open_count() >= self.cfg.live_signal_max_open_positions:
            return False
        return True

    def _open_signal(self, snap, result, baseline_token_raw: int = 0):
        with self.connect() as c:
            cur = c.execute(
                """INSERT INTO live_signal_positions
                   (chain,token_address,token_symbol,opened_ts,entry_price_usd,
                    entry_score,entry_liquidity,entry_market_cap,last_price_usd,
                    last_score,status,baseline_token_raw,wallet_buy_confirmed,
                    confirmed_token_raw,sell_requested,wallet_sell_confirmed)
                   VALUES(?,?,?,?,?,?,?,?,?,?, 'OPEN',?,0,0,0,0)""",
                (
                    snap.chain, snap.token_address, snap.token_symbol,
                    int(time.time()), float(snap.price_usd), int(result.score),
                    float(snap.liquidity_usd or 0), float(snap.market_cap or 0),
                    float(snap.price_usd), int(result.score),
                    int(baseline_token_raw or 0),
                ),
            )
            return cur.lastrowid

    def _update_mark(self, pid: int, snap, result):
        with self.connect() as c:
            c.execute(
                """UPDATE live_signal_positions
                   SET last_price_usd=?,last_score=?
                   WHERE id=? AND status='OPEN'""",
                (float(snap.price_usd or 0), int(result.score), int(pid)),
            )

    def _close_signal(self, pid: int, reason: str):
        with self.connect() as c:
            c.execute(
                """UPDATE live_signal_positions
                   SET status='CLOSED',closed_ts=?,exit_reason=?
                   WHERE id=? AND status='OPEN'""",
                (int(time.time()), reason, int(pid)),
            )

    def _confirm_wallet_buy(
        self,
        pid: int,
        entry_price_usd: float,
        confirmed_token_raw: int,
    ):
        with self.connect() as c:
            c.execute(
                """UPDATE live_signal_positions
                   SET wallet_buy_confirmed=1,
                       confirmed_entry_price_usd=?,
                       confirmed_entry_ts=?,
                       confirmed_token_raw=?
                   WHERE id=? AND status='OPEN'""",
                (
                    float(entry_price_usd or 0),
                    int(time.time()),
                    int(confirmed_token_raw or 0),
                    int(pid),
                ),
            )

    def _request_sell(self, pid: int):
        with self.connect() as c:
            c.execute(
                """UPDATE live_signal_positions
                   SET sell_requested=1,sell_signal_ts=?
                   WHERE id=? AND status='OPEN'""",
                (int(time.time()), int(pid)),
            )

    def _close_confirmed(self, pid: int, reason: str):
        with self.connect() as c:
            c.execute(
                """UPDATE live_signal_positions
                   SET status='CLOSED',closed_ts=?,exit_reason=?,
                       wallet_sell_confirmed=1
                   WHERE id=? AND status='OPEN'""",
                (int(time.time()), reason, int(pid)),
            )

    def _cancel_unfilled(self, pid: int):
        with self.connect() as c:
            c.execute(
                """UPDATE live_signal_positions
                   SET status='CLOSED',closed_ts=?,
                       exit_reason='BUY SIGNAL EXPIRED / NOT CONFIRMED'
                   WHERE id=? AND status='OPEN'""",
                (int(time.time()), int(pid)),
            )

    async def _wallet_balance(self, mint: str) -> dict:
        return await self.market.solana_token_balance(
            self.cfg.wallet_public_address,
            mint,
            self.cfg.solana_rpc_url,
        )

    async def _estimated_fill_price(self, p: dict, snap, current_raw: int) -> tuple[float, str]:
        fallback = float(snap.price_usd or p.get("entry_price_usd") or 0)
        try:
            fill = await self.market.solana_recent_buy_fill(
                self.cfg.wallet_public_address,
                snap.token_address,
                int(p.get("opened_ts") or 0),
                self.cfg.solana_rpc_url,
            )
            if not fill:
                return fallback, ""

            token_ui = float(fill.get("token_ui") or 0)
            sol_spent = float(fill.get("sol_spent") or 0)
            if token_ui <= 0 or sol_spent <= 0:
                return fallback, str(fill.get("signature") or "")

            sol_usd = await self.market.solana_sol_price_usd()
            if sol_usd <= 0:
                return fallback, str(fill.get("signature") or "")

            estimated = (sol_spent * sol_usd) / token_ui
            if estimated <= 0:
                return fallback, str(fill.get("signature") or "")
            return estimated, str(fill.get("signature") or "")
        except Exception:
            return fallback, ""

    async def _send_buy_confirmed(self, snap, entry_price: float, acquired_raw: int, signature: str):
        if not self.telegram.chat_id:
            return
        sig_line = (
            f"\nSignature: <code>{html.escape(signature[:18])}…</code>"
            if signature else ""
        )
        await self.telegram.send(
            f"✅ <b>BUY CONFIRMED ON-CHAIN</b>\n\n"
            f"<b>{html.escape(snap.token_symbol)}</b>\n"
            f"Wallet token increase detected: <b>{int(acquired_raw):,} raw units</b>\n"
            f"Tracked entry price: <b>${entry_price:.10f}</b>{sig_line}\n\n"
            f"🎯 Monitoring starts now from this confirmed entry.\n"
            f"Profit target: <b>+{self.cfg.live_signal_take_profit_percent:.0f}%</b>\n"
            f"Risk exit: <b>-{self.cfg.live_signal_stop_loss_percent:.0f}%</b>\n\n"
            "ℹ️ Entry price is estimated from the confirmed on-chain wallet change "
            "and recent swap transaction; first-time token-account rent can make it slightly conservative.",
            urgent=True,
        )

    async def _send_sell_confirmed(self, snap):
        if not self.telegram.chat_id:
            return
        await self.telegram.send(
            f"✅ <b>SELL CONFIRMED ON-CHAIN</b>\n\n"
            f"<b>{html.escape(snap.token_symbol)}</b>\n"
            "The wallet token balance has returned to its pre-signal level.\n\n"
            "🔎 Position closed. Fresh prospect search starts immediately.",
            urgent=True,
        )

    async def _send_signal_expired(self, snap):
        if not self.telegram.chat_id:
            return
        await self.telegram.send(
            f"⌛ <b>BUY SIGNAL EXPIRED</b>\n\n"
            f"<b>{html.escape(snap.token_symbol)}</b>\n"
            "No wallet purchase was detected within the confirmation window.\n"
            "The bot will search for another prospect.",
        )

    def _jupiter_url(self, input_asset: str, output_asset: str) -> str:
        # Jupiter's current Spot UI uses explicit buy/sell mint query parameters.
        # Native SOL must use its wrapped-SOL mint address in the URL.
        sol_mint = "So11111111111111111111111111111111111111112"
        sell_mint = sol_mint if input_asset == "SOL" else input_asset
        buy_mint = sol_mint if output_asset == "SOL" else output_asset
        return f"https://jup.ag/?buy={buy_mint}&sell={sell_mint}"

    def _solflare_browse_url(self, target_url: str) -> str:
        encoded_target = quote(target_url, safe="")
        ref = quote("https://t.me/Memeprospects_bot", safe="")
        return f"https://solflare.com/ul/v1/browse/{encoded_target}?ref={ref}"

    def _buy_buttons(self, token_mint: str):
        jup = self._jupiter_url("SOL", token_mint)
        return [
            [{"text": f"🚨 BUY NOW — {self.cfg.live_buy_sol:.3f} SOL",
              "url": self._solflare_browse_url(jup)}],
            [{"text": "🪐 Open Jupiter", "url": jup}],
        ]

    def _sell_buttons(self, token_mint: str):
        jup = self._jupiter_url(token_mint, "SOL")
        return [
            [{"text": "🚨 SELL ALL NOW",
              "url": self._solflare_browse_url(jup)}],
            [{"text": "🪐 Open Jupiter", "url": jup}],
        ]

    def _has_signal_history(self, chain: str, address: str) -> bool:
        with self.connect() as c:
            row = c.execute(
                """SELECT 1 FROM live_signal_positions
                   WHERE chain=? AND token_address=? LIMIT 1""",
                (chain, address),
            ).fetchone()
            return bool(row)

    async def _send_buy_signal(self, snap, result):
        if not self.telegram.chat_id:
            return
        await self.telegram.send(
            f"🚨🚨 <b>ACTION REQUIRED — BUY SETUP READY</b> 🚨🚨\n\n"
            f"🟢 <b>{html.escape(snap.token_name)} ({html.escape(snap.token_symbol)})</b>\n"
            f"Prospect score: <b>{result.score}/100</b>\n"
            f"Signal price: <b>${snap.price_usd:.10f}</b>\n"
            f"Market cap: <b>${snap.market_cap:,.0f}</b>\n"
            f"Liquidity: <b>${snap.liquidity_usd:,.0f}</b>\n\n"
            f"💰 Planned buy: <b>{self.cfg.live_buy_sol:.3f} SOL</b>\n"
            f"🎯 Profit target: <b>+{self.cfg.live_signal_take_profit_percent:.0f}% → SELL ALL</b>\n"
            f"🛑 Risk exit: <b>-{self.cfg.live_signal_stop_loss_percent:.0f}% → EXIT ALL</b>\n\n"
            f"<b>WHAT TO DO:</b> Tap <b>BUY NOW</b>. Jupiter opens in Solflare. "
            f"Confirm the amount is <b>{self.cfg.live_buy_sol:.3f} SOL</b>, check the quote, "
            f"then approve the swap.\n\n"
            "🔐 One wallet approval is required. The bot never signs the transaction.",
            buttons=self._buy_buttons(snap.token_address),
            urgent=True,
        )

    async def _send_sell_signal(self, snap, ret: float, reason: str):
        if not self.telegram.chat_id:
            return

        target_hit = ret >= self.cfg.live_signal_take_profit_percent
        headline = (
            "🎯🚨 PROFIT TARGET HIT — SELL ALL NOW"
            if target_hit else
            "🛑🚨 EXIT TRIGGERED — SELL ALL NOW"
        )

        await self.telegram.send(
            f"<b>{headline}</b>\n\n"
            f"<b>{html.escape(snap.token_symbol)}</b>\n"
            f"Signal return: <b>{ret:+.1f}%</b>\n"
            f"Reason: <b>{html.escape(reason)}</b>\n\n"
            "<b>WHAT TO DO:</b> Tap <b>SELL ALL NOW</b>, select <b>MAX</b> in Jupiter, "
            "review the quote, and approve the swap in Solflare.\n\n"
            "🔎 The bot immediately begins searching for a different prospect after this exit signal.\n"
            "🔐 One wallet approval is required. No automatic transaction is submitted.",
            buttons=self._sell_buttons(snap.token_address),
            urgent=True,
        )

    async def manage(self, snap, result) -> bool:
        """
        Read-only wallet-aware position manager.
        Returns True only when a position is actually closed/cancelled, which
        triggers the scanner's immediate fresh-prospect rescan.
        """
        p = self.get_open(snap.chain, snap.token_address)

        if p:
            self._update_mark(p["id"], snap, result)

            # If wallet tracking is disabled, preserve signal-only behavior.
            if not self.cfg.wallet_trade_tracking_enabled:
                entry = float(p.get("entry_price_usd") or 0)
                if entry <= 0 or not snap.price_usd or snap.price_usd <= 0:
                    return False
                ret = ((float(snap.price_usd) / entry) - 1.0) * 100
                if not int(p.get("sell_requested") or 0):
                    if ret >= self.cfg.live_signal_take_profit_percent:
                        self._request_sell(p["id"])
                        await self._send_sell_signal(
                            snap, ret,
                            f"TAKE PROFIT +{self.cfg.live_signal_take_profit_percent:.0f}% → SELL ALL",
                        )
                    elif ret <= -self.cfg.live_signal_stop_loss_percent:
                        self._request_sell(p["id"])
                        await self._send_sell_signal(
                            snap, ret,
                            f"STOP SIGNAL -{self.cfg.live_signal_stop_loss_percent:.0f}% → EXIT ALL",
                        )
                return False

            try:
                wallet = await self._wallet_balance(snap.token_address)
            except Exception:
                return False

            current_raw = int(wallet.get("raw") or 0)
            baseline_raw = int(p.get("baseline_token_raw") or 0)
            buy_confirmed = bool(int(p.get("wallet_buy_confirmed") or 0))
            sell_requested = bool(int(p.get("sell_requested") or 0))

            # Waiting for the user's Solflare/Jupiter BUY approval to actually land.
            if not buy_confirmed:
                if current_raw > baseline_raw:
                    acquired_raw = current_raw - baseline_raw
                    entry_price, signature = await self._estimated_fill_price(
                        p, snap, current_raw
                    )
                    self._confirm_wallet_buy(
                        p["id"], entry_price, acquired_raw
                    )
                    await self._send_buy_confirmed(
                        snap, entry_price, acquired_raw, signature
                    )
                    return False

                age = int(time.time()) - int(p.get("opened_ts") or 0)
                if age >= self.cfg.wallet_buy_confirm_timeout_seconds:
                    self._cancel_unfilled(p["id"])
                    await self._send_signal_expired(snap)
                    return True
                return False

            # A confirmed position exists. If balance has already returned to the
            # pre-signal baseline, the wallet has exited (even if manually).
            if current_raw <= baseline_raw:
                reason = (
                    "WALLET SELL CONFIRMED"
                    if sell_requested else
                    "MANUAL WALLET EXIT DETECTED"
                )
                self._close_confirmed(p["id"], reason)
                await self._send_sell_confirmed(snap)
                return True

            # Once a sell signal has been sent, wait for the wallet balance to prove
            # that the user actually completed it. Do not pretend the trade is closed.
            if sell_requested:
                return False

            entry = float(
                p.get("confirmed_entry_price_usd")
                or p.get("entry_price_usd")
                or 0
            )
            if entry <= 0 or not snap.price_usd or snap.price_usd <= 0:
                return False

            ret = ((float(snap.price_usd) / entry) - 1.0) * 100

            exit_reason = ""
            if ret >= self.cfg.live_signal_take_profit_percent:
                exit_reason = (
                    f"TAKE PROFIT +{self.cfg.live_signal_take_profit_percent:.0f}% "
                    "→ SELL ALL"
                )
            elif ret <= -self.cfg.live_signal_stop_loss_percent:
                exit_reason = (
                    f"STOP SIGNAL -{self.cfg.live_signal_stop_loss_percent:.0f}% "
                    "→ EXIT ALL"
                )
            elif result.hard_block or result.score < self.cfg.live_exit_score:
                exit_reason = (
                    f"RISK / SCORE COLLAPSE {result.score}/100 → EXIT ALL"
                )

            if exit_reason:
                self._request_sell(p["id"])
                await self._send_sell_signal(snap, ret, exit_reason)

            return False

        # No signal position yet.
        if self._signal_buy_allowed(snap, result):
            baseline_raw = 0
            if self.cfg.wallet_trade_tracking_enabled:
                try:
                    wallet = await self._wallet_balance(snap.token_address)
                    baseline_raw = int(wallet.get("raw") or 0)
                except Exception:
                    # If wallet RPC is temporarily unavailable, don't create a
                    # wallet-tracked signal we cannot safely verify.
                    return False

            self._open_signal(
                snap, result, baseline_token_raw=baseline_raw
            )
            await self._send_buy_signal(snap, result)

        return False

