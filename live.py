from __future__ import annotations
import html, sqlite3, time
from urllib.parse import quote

USER_LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_live_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_chat_id TEXT NOT NULL,
  wallet_address TEXT NOT NULL,
  chain TEXT NOT NULL,
  token_address TEXT NOT NULL,
  token_symbol TEXT,
  opened_ts INTEGER NOT NULL,
  closed_ts INTEGER,
  signal_entry_price_usd REAL NOT NULL,
  entry_score INTEGER NOT NULL,
  entry_liquidity REAL,
  entry_market_cap REAL,
  baseline_token_raw INTEGER NOT NULL DEFAULT 0,
  wallet_buy_confirmed INTEGER NOT NULL DEFAULT 0,
  confirmed_entry_price_usd REAL,
  confirmed_entry_ts INTEGER,
  confirmed_token_raw INTEGER NOT NULL DEFAULT 0,
  last_price_usd REAL,
  last_score INTEGER,
  sell_requested INTEGER NOT NULL DEFAULT 0,
  sell_signal_ts INTEGER,
  status TEXT NOT NULL DEFAULT 'OPEN',
  exit_reason TEXT,
  exit_price_usd REAL,
  realized_return_pct REAL,
  UNIQUE(user_chat_id,chain,token_address,status)
);
CREATE INDEX IF NOT EXISTS idx_user_live_status
ON user_live_positions(user_chat_id,status,opened_ts DESC);
CREATE INDEX IF NOT EXISTS idx_user_live_token
ON user_live_positions(chain,token_address,status);
"""

class LiveTrader:
    """
    Multi-user live-market signal engine.

    It never signs or submits a transaction.
    Every real BUY/SELL still requires the user to approve in Solflare.
    Public wallet data is used only to confirm what actually happened on-chain.
    """

    def __init__(self, cfg, store, market, telegram, accounts):
        self.cfg = cfg
        self.store = store
        self.market = market
        self.telegram = telegram
        self.accounts = accounts
        with self.connect() as c:
            c.executescript(USER_LIVE_SCHEMA)

    def connect(self):
        return sqlite3.connect(self.store.path)

    def execution_ready(self) -> bool:
        return False

    def is_paused(self) -> bool:
        # Kept only for compatibility. Multi-user pause is per account.
        return False

    def set_paused(self, paused: bool):
        return None

    def open_positions(self, user_chat_id: str | None = None):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            if user_chat_id:
                rows = c.execute(
                    """SELECT * FROM user_live_positions
                       WHERE user_chat_id=? AND status='OPEN'
                       ORDER BY opened_ts DESC""",
                    (str(user_chat_id),),
                ).fetchall()
            else:
                rows = c.execute(
                    """SELECT * FROM user_live_positions
                       WHERE status='OPEN' ORDER BY opened_ts DESC"""
                ).fetchall()
            return [dict(x) for x in rows]

    def closed_positions(self, user_chat_id: str, limit: int = 10):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                """SELECT * FROM user_live_positions
                   WHERE user_chat_id=? AND status='CLOSED'
                   ORDER BY closed_ts DESC LIMIT ?""",
                (str(user_chat_id), int(limit)),
            ).fetchall()
            return [dict(x) for x in rows]

    def open_count(self, user_chat_id: str) -> int:
        with self.connect() as c:
            row = c.execute(
                """SELECT COUNT(*) FROM user_live_positions
                   WHERE user_chat_id=? AND status='OPEN'""",
                (str(user_chat_id),),
            ).fetchone()
        return int(row[0] or 0)

    def get_open(self, user_chat_id: str, chain: str, address: str):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                """SELECT * FROM user_live_positions
                   WHERE user_chat_id=? AND chain=? AND token_address=?
                   AND status='OPEN' LIMIT 1""",
                (str(user_chat_id), chain, address),
            ).fetchone()
            return dict(row) if row else None

    def _open_for_token(self, chain: str, address: str):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                """SELECT * FROM user_live_positions
                   WHERE chain=? AND token_address=? AND status='OPEN'""",
                (chain, address),
            ).fetchall()
            return [dict(x) for x in rows]

    def _has_history(self, user_chat_id: str, chain: str, address: str) -> bool:
        with self.connect() as c:
            row = c.execute(
                """SELECT 1 FROM user_live_positions
                   WHERE user_chat_id=? AND chain=? AND token_address=? LIMIT 1""",
                (str(user_chat_id), chain, address),
            ).fetchone()
        return bool(row)

    def _candidate_ok(self, snap, result) -> bool:
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
        if snap.price_change_m5 > self.cfg.live_max_5m_move_percent:
            return False
        return True

    def _open_signal(self, user: dict, snap, result, baseline_raw: int):
        with self.connect() as c:
            cur = c.execute(
                """INSERT INTO user_live_positions
                   (user_chat_id,wallet_address,chain,token_address,token_symbol,
                    opened_ts,signal_entry_price_usd,entry_score,entry_liquidity,
                    entry_market_cap,baseline_token_raw,last_price_usd,last_score,status)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN')""",
                (
                    str(user["chat_id"]),
                    str(user["wallet_address"]),
                    snap.chain,
                    snap.token_address,
                    snap.token_symbol,
                    int(time.time()),
                    float(snap.price_usd),
                    int(result.score),
                    float(snap.liquidity_usd or 0),
                    float(snap.market_cap or 0),
                    int(baseline_raw or 0),
                    float(snap.price_usd),
                    int(result.score),
                ),
            )
            return cur.lastrowid

    def _update_mark(self, pid: int, snap, result):
        with self.connect() as c:
            c.execute(
                """UPDATE user_live_positions SET last_price_usd=?,last_score=?
                   WHERE id=? AND status='OPEN'""",
                (float(snap.price_usd or 0), int(result.score), int(pid)),
            )

    def _confirm_buy(self, pid: int, price: float, token_raw: int):
        with self.connect() as c:
            c.execute(
                """UPDATE user_live_positions
                   SET wallet_buy_confirmed=1,confirmed_entry_price_usd=?,
                       confirmed_entry_ts=?,confirmed_token_raw=?
                   WHERE id=? AND status='OPEN'""",
                (float(price or 0), int(time.time()), int(token_raw or 0), int(pid)),
            )

    def _request_sell(self, pid: int):
        with self.connect() as c:
            c.execute(
                """UPDATE user_live_positions
                   SET sell_requested=1,sell_signal_ts=?
                   WHERE id=? AND status='OPEN'""",
                (int(time.time()), int(pid)),
            )

    def _close(self, pid: int, reason: str, exit_price: float = 0, ret: float | None = None):
        with self.connect() as c:
            c.execute(
                """UPDATE user_live_positions
                   SET status='CLOSED',closed_ts=?,exit_reason=?,exit_price_usd=?,
                       realized_return_pct=?
                   WHERE id=? AND status='OPEN'""",
                (
                    int(time.time()),
                    str(reason),
                    float(exit_price or 0),
                    None if ret is None else float(ret),
                    int(pid),
                ),
            )

    def _jupiter_url(self, input_asset: str, output_asset: str) -> str:
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

    async def _wallet_balance(self, wallet: str, mint: str) -> dict:
        return await self.market.solana_token_balance(
            wallet, mint, self.cfg.solana_rpc_url
        )

    async def _entry_price(self, p: dict, snap) -> tuple[float, str]:
        fallback = float(snap.price_usd or p.get("signal_entry_price_usd") or 0)
        try:
            fill = await self.market.solana_recent_buy_fill(
                str(p["wallet_address"]),
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

            price = (sol_spent * sol_usd) / token_ui
            return (price if price > 0 else fallback), str(fill.get("signature") or "")
        except Exception:
            return fallback, ""

    async def _send_buy(self, user: dict, snap, result):
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
            f"Tap <b>BUY NOW</b>, verify the coin and amount in Solflare, then approve.\n"
            "The bot waits for your public wallet to confirm the purchase before tracking P/L.",
            str(user["chat_id"]),
            buttons=self._buy_buttons(snap.token_address),
            urgent=True,
        )

    async def _send_buy_confirmed(self, p: dict, snap, entry_price: float, signature: str):
        sig_line = (
            f"\nTransaction: <code>{html.escape(signature[:18])}…</code>"
            if signature else ""
        )
        await self.telegram.send(
            f"✅ <b>BUY CONFIRMED ON-CHAIN</b>\n\n"
            f"<b>{html.escape(snap.token_symbol)}</b>\n"
            f"Tracked entry: <b>${entry_price:.10f}</b>{sig_line}\n"
            f"🎯 +{self.cfg.live_signal_take_profit_percent:.0f}% → SELL ALL alert\n"
            f"🛑 -{self.cfg.live_signal_stop_loss_percent:.0f}% → EXIT ALL alert\n\n"
            "Monitoring has started from the confirmed wallet purchase.",
            str(p["user_chat_id"]),
            urgent=True,
        )

    async def _send_sell(self, p: dict, snap, ret: float, reason: str):
        target = ret >= self.cfg.live_signal_take_profit_percent
        headline = (
            "🎯🚨 PROFIT TARGET HIT — SELL ALL NOW"
            if target else "🛑🚨 EXIT TRIGGERED — SELL ALL NOW"
        )
        await self.telegram.send(
            f"<b>{headline}</b>\n\n"
            f"<b>{html.escape(snap.token_symbol)}</b>\n"
            f"Tracked return: <b>{ret:+.1f}%</b>\n"
            f"Reason: <b>{html.escape(reason)}</b>\n\n"
            "Tap <b>SELL ALL NOW</b>, choose MAX, verify the quote and approve in Solflare.\n"
            "The bot will not mark the trade closed until the public wallet balance confirms the exit.",
            str(p["user_chat_id"]),
            buttons=self._sell_buttons(snap.token_address),
            urgent=True,
        )

    async def _send_sell_confirmed(self, p: dict, snap, ret: float):
        await self.telegram.send(
            f"✅ <b>SELL CONFIRMED ON-CHAIN</b>\n\n"
            f"<b>{html.escape(snap.token_symbol)}</b>\n"
            f"Tracked result: <b>{ret:+.1f}%</b>\n"
            "Position closed. The bot can now search for the next prospect.",
            str(p["user_chat_id"]),
            urgent=True,
        )

    async def _send_expired(self, p: dict, snap):
        await self.telegram.send(
            f"⌛ <b>BUY SIGNAL EXPIRED</b>\n\n"
            f"<b>{html.escape(snap.token_symbol)}</b>\n"
            "No wallet purchase was detected within the confirmation window.",
            str(p["user_chat_id"]),
        )

    async def _manage_position(self, p: dict, snap, result) -> bool:
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
                await self._send_buy_confirmed(p, snap, entry, sig)
                return False

            if int(time.time()) - int(p.get("opened_ts") or 0) >= self.cfg.wallet_buy_confirm_timeout_seconds:
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
            if entry > 0 and snap.price_usd and snap.price_usd > 0 else 0.0
        )

        # Public wallet proves an exit, including a manual one.
        if current_raw <= baseline:
            reason = "WALLET SELL CONFIRMED" if sell_requested else "MANUAL WALLET EXIT DETECTED"
            self._close(p["id"], reason, float(snap.price_usd or 0), ret)
            await self._send_sell_confirmed(p, snap, ret)
            return True

        if sell_requested:
            return False

        reason = ""
        if ret >= self.cfg.live_signal_take_profit_percent:
            reason = f"TAKE PROFIT +{self.cfg.live_signal_take_profit_percent:.0f}%"
        elif ret <= -self.cfg.live_signal_stop_loss_percent:
            reason = f"STOP SIGNAL -{self.cfg.live_signal_stop_loss_percent:.0f}%"
        elif result.hard_block or result.score < self.cfg.live_exit_score:
            reason = f"RISK / SCORE COLLAPSE {result.score}/100"

        if reason:
            self._request_sell(p["id"])
            await self._send_sell(p, snap, ret, reason)
        return False

    async def manage(self, snap, result) -> bool:
        """
        Manage every user's existing position for this token, then offer new
        signals to eligible active subscribers. Returns True when any position
        actually closes/expires so Scanner can request a fast rescan.
        """
        closed_any = False

        # Existing positions continue to be protected even if a subscription expires
        # or the user pauses new signals.
        for p in self._open_for_token(snap.chain, snap.token_address):
            if await self._manage_position(p, snap, result):
                closed_any = True

        if not self._candidate_ok(snap, result):
            return closed_any

        for user in self.accounts.trade_users():
            chat_id = str(user["chat_id"])
            if self.get_open(chat_id, snap.chain, snap.token_address):
                continue
            if self._has_history(chat_id, snap.chain, snap.token_address):
                continue
            if self.open_count(chat_id) >= self.cfg.live_signal_max_open_positions:
                continue

            try:
                bal = await self._wallet_balance(
                    str(user["wallet_address"]), snap.token_address
                )
            except Exception:
                continue

            self._open_signal(user, snap, result, int(bal.get("raw") or 0))
            await self._send_buy(user, snap, result)

        return closed_any

    def summary(self, user_chat_id: str | None = None) -> dict:
        return {
            "open": self.open_positions(user_chat_id),
            "paused": False,
        }
