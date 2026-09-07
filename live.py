from __future__ import annotations
import html, sqlite3, time
from pathlib import Path

SOL_MINT = "So11111111111111111111111111111111111111112"

LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chain TEXT NOT NULL,
  token_address TEXT NOT NULL,
  token_symbol TEXT,
  opened_ts INTEGER NOT NULL,
  closed_ts INTEGER,
  entry_price_usd REAL NOT NULL,
  entry_score INTEGER NOT NULL,
  entry_market_cap REAL,
  entry_liquidity REAL,
  input_sol_lamports INTEGER NOT NULL,
  initial_token_raw INTEGER NOT NULL,
  remaining_token_raw INTEGER NOT NULL,
  realized_sol_lamports INTEGER NOT NULL DEFAULT 0,
  realized_pnl_lamports INTEGER NOT NULL DEFAULT 0,
  tp1_done INTEGER NOT NULL DEFAULT 0,
  tp2_done INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'OPEN',
  exit_reason TEXT,
  buy_signature TEXT,
  last_price_usd REAL,
  last_score INTEGER,
  last_liquidity REAL,
  UNIQUE(chain, token_address, status)
);
CREATE INDEX IF NOT EXISTS idx_live_positions_status
ON live_positions(status, opened_ts DESC);

CREATE TABLE IF NOT EXISTS live_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  position_id INTEGER NOT NULL,
  side TEXT NOT NULL,
  token_raw INTEGER NOT NULL,
  sol_lamports INTEGER NOT NULL,
  pnl_sol_lamports INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  signature TEXT
);
CREATE INDEX IF NOT EXISTS idx_live_trades_position
ON live_trades(position_id, ts);
"""

class LiveTrader:
    def __init__(self, cfg, store, market, telegram):
        self.cfg = cfg
        self.store = store
        self.market = market
        self.telegram = telegram
        with self.connect() as c:
            c.executescript(LIVE_SCHEMA)

    def connect(self):
        return sqlite3.connect(self.store.path)

    def is_paused(self) -> bool:
        return self.store.get_setting("live_trading_paused", "1") != "0"

    def set_paused(self, paused: bool):
        self.store.set_setting("live_trading_paused", "1" if paused else "0")

    def signer_status(self) -> dict:
        return self.market.signer_status(
            self.cfg.solana_private_key_b58,
            self.cfg.wallet_public_address,
        )

    def execution_ready(self) -> bool:
        signer = self.signer_status()
        return (
            self.cfg.live_trading_enabled
            and signer["matches"]
            and not self.is_paused()
        )

    def get_open(self, chain: str, address: str):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                """SELECT * FROM live_positions
                   WHERE chain=? AND token_address=? AND status='OPEN' LIMIT 1""",
                (chain, address),
            ).fetchone()
            return dict(row) if row else None

    def open_positions(self):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM live_positions WHERE status='OPEN' ORDER BY opened_ts DESC"
            ).fetchall()
            return [dict(x) for x in rows]

    def open_count(self) -> int:
        with self.connect() as c:
            return int(c.execute(
                "SELECT COUNT(*) FROM live_positions WHERE status='OPEN'"
            ).fetchone()[0] or 0)

    def daily_spend_lamports(self) -> int:
        start = int(time.time()) - (int(time.time()) % 86400)
        with self.connect() as c:
            return int(c.execute(
                "SELECT COALESCE(SUM(sol_lamports),0) FROM live_trades "
                "WHERE side='BUY' AND ts>=?",
                (start,),
            ).fetchone()[0] or 0)

    def daily_realized_pnl_lamports(self) -> int:
        start = int(time.time()) - (int(time.time()) % 86400)
        with self.connect() as c:
            return int(c.execute(
                "SELECT COALESCE(SUM(pnl_sol_lamports),0) FROM live_trades "
                "WHERE side='SELL' AND ts>=?",
                (start,),
            ).fetchone()[0] or 0)

    def summary(self) -> dict:
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            opens = [dict(x) for x in c.execute(
                "SELECT * FROM live_positions WHERE status='OPEN' ORDER BY opened_ts DESC"
            ).fetchall()]
            closed = int(c.execute(
                "SELECT COUNT(*) FROM live_positions WHERE status='CLOSED'"
            ).fetchone()[0] or 0)
            realized = int(c.execute(
                "SELECT COALESCE(SUM(realized_pnl_lamports),0) FROM live_positions"
            ).fetchone()[0] or 0)
        return {
            "open": opens,
            "closed_count": closed,
            "realized_pnl_sol": realized / 1_000_000_000,
            "daily_spend_sol": self.daily_spend_lamports() / 1_000_000_000,
            "daily_realized_pnl_sol": self.daily_realized_pnl_lamports() / 1_000_000_000,
            "paused": self.is_paused(),
        }

    def _update_mark(self, p, snap, result):
        with self.connect() as c:
            c.execute(
                """UPDATE live_positions
                   SET last_price_usd=?,last_score=?,last_liquidity=?
                   WHERE id=? AND status='OPEN'""",
                (
                    float(snap.price_usd or 0), int(result.score),
                    float(snap.liquidity_usd or 0), int(p["id"]),
                ),
            )

    def _open_position(self, snap, result, sol_lamports, token_raw, signature):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            cur = c.execute(
                """INSERT INTO live_positions
                   (chain,token_address,token_symbol,opened_ts,entry_price_usd,entry_score,
                    entry_market_cap,entry_liquidity,input_sol_lamports,initial_token_raw,
                    remaining_token_raw,buy_signature,last_price_usd,last_score,last_liquidity)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snap.chain, snap.token_address, snap.token_symbol, int(time.time()),
                    float(snap.price_usd), int(result.score), float(snap.market_cap or 0),
                    float(snap.liquidity_usd or 0), int(sol_lamports), int(token_raw),
                    int(token_raw), str(signature or ""), float(snap.price_usd),
                    int(result.score), float(snap.liquidity_usd or 0),
                ),
            )
            pid = cur.lastrowid
            c.execute(
                """INSERT INTO live_trades
                   (ts,position_id,side,token_raw,sol_lamports,pnl_sol_lamports,reason,signature)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    int(time.time()), pid, "BUY", int(token_raw), int(sol_lamports),
                    0, "AUTO LIVE BUY", str(signature or ""),
                ),
            )
            return dict(c.execute(
                "SELECT * FROM live_positions WHERE id=?", (pid,)
            ).fetchone())

    def _record_sell(self, p, token_raw, sol_lamports, reason, signature):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            p = dict(c.execute(
                "SELECT * FROM live_positions WHERE id=? AND status='OPEN'",
                (int(p["id"]),),
            ).fetchone() or {})
            if not p:
                return None

            remaining = int(p["remaining_token_raw"] or 0)
            initial = int(p["initial_token_raw"] or 0)
            sold = min(max(int(token_raw), 0), remaining)
            if sold <= 0 or initial <= 0:
                return None

            cost_slice = round(int(p["input_sol_lamports"]) * (sold / initial))
            proceeds = int(sol_lamports or 0)
            pnl = proceeds - cost_slice
            new_remaining = remaining - sold
            closed = new_remaining <= 0

            c.execute(
                """INSERT INTO live_trades
                   (ts,position_id,side,token_raw,sol_lamports,pnl_sol_lamports,reason,signature)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    int(time.time()), int(p["id"]), "SELL", sold, proceeds,
                    pnl, reason, str(signature or ""),
                ),
            )
            c.execute(
                """UPDATE live_positions
                   SET remaining_token_raw=?,
                       realized_sol_lamports=realized_sol_lamports+?,
                       realized_pnl_lamports=realized_pnl_lamports+?,
                       status=?,closed_ts=?,exit_reason=?
                   WHERE id=?""",
                (
                    max(0, new_remaining), proceeds, pnl,
                    "CLOSED" if closed else "OPEN",
                    int(time.time()) if closed else None,
                    reason if closed else p.get("exit_reason"),
                    int(p["id"]),
                ),
            )
            updated = dict(c.execute(
                "SELECT * FROM live_positions WHERE id=?", (int(p["id"]),)
            ).fetchone())
            updated["sell_sol_lamports"] = proceeds
            updated["sell_pnl_lamports"] = pnl
            updated["sell_reason"] = reason
            updated["sell_signature"] = str(signature or "")
            return updated

    def _mark_tp(self, pid: int, which: int):
        col = "tp1_done" if which == 1 else "tp2_done"
        with self.connect() as c:
            c.execute(f"UPDATE live_positions SET {col}=1 WHERE id=?", (int(pid),))

    def _buy_allowed(self, snap, result) -> bool:
        if not self.execution_ready():
            return False
        if snap.chain != "solana":
            return False
        if result.hard_block or result.score < self.cfg.live_buy_score:
            return False
        if not snap.price_usd or snap.price_usd <= 0:
            return False
        if snap.liquidity_usd < self.cfg.live_min_liquidity_usd:
            return False
        if snap.market_cap <= 0 or snap.market_cap > self.cfg.live_max_market_cap_usd:
            return False
        if snap.price_change_m5 > self.cfg.live_max_5m_move_percent:
            return False
        if self.get_open(snap.chain, snap.token_address):
            return False
        if self.open_count() >= self.cfg.live_max_open_positions:
            return False

        buy_lamports = int(self.cfg.live_buy_sol * 1_000_000_000)
        daily_cap = int(self.cfg.live_daily_spend_sol * 1_000_000_000)
        if self.daily_spend_lamports() + buy_lamports > daily_cap:
            return False

        loss_cap = int(self.cfg.live_daily_loss_sol * 1_000_000_000)
        if self.daily_realized_pnl_lamports() <= -loss_cap:
            return False
        return True

    async def _send_buy_message(self, snap, result, sol_lamports, token_raw, signature):
        if not self.telegram.chat_id:
            return
        sig = html.escape(str(signature or ""))
        tx = f'\n<a href="https://solscan.io/tx/{sig}">View transaction</a>' if sig else ""
        await self.telegram.send(
            f"💰🟢 <b>REAL BUY EXECUTED</b>\n\n"
            f"<b>{html.escape(snap.token_name)} ({html.escape(snap.token_symbol)})</b>\n"
            f"Score: <b>{result.score}/100</b>\n"
            f"Spent: <b>{sol_lamports / 1_000_000_000:.6f} SOL</b>\n"
            f"Entry price: <b>${snap.price_usd:.10f}</b>\n"
            f"Liquidity: <b>${snap.liquidity_usd:,.0f}</b>\n"
            f"Market cap: <b>${snap.market_cap:,.0f}</b>"
            + tx
            + "\n\n⚠️ <b>REAL FUNDS WERE USED.</b>"
        )

    async def _send_sell_message(self, symbol, sold):
        if not self.telegram.chat_id or not sold:
            return
        proceeds = int(sold.get("sell_sol_lamports") or 0) / 1_000_000_000
        pnl = int(sold.get("sell_pnl_lamports") or 0) / 1_000_000_000
        initial = int(sold.get("initial_token_raw") or 0)
        remaining = int(sold.get("remaining_token_raw") or 0)
        rem = remaining / initial * 100 if initial else 0
        sig = html.escape(str(sold.get("sell_signature") or ""))
        tx = f'\n<a href="https://solscan.io/tx/{sig}">View transaction</a>' if sig else ""
        icon = "🟢" if pnl >= 0 else "🔴"
        await self.telegram.send(
            f"💰 <b>REAL SELL EXECUTED</b>\n\n"
            f"<b>{html.escape(symbol)}</b>\n"
            f"Received: <b>{proceeds:.6f} SOL</b>\n"
            f"{icon} P/L on this sale: <b>{pnl:+.6f} SOL</b>\n"
            f"Remaining: <b>{rem:.0f}%</b>\n"
            f"Reason: <b>{html.escape(str(sold.get('sell_reason') or 'Rule triggered'))}</b>"
            + tx
            + "\n\n⚠️ <b>REAL FUNDS WERE MOVED.</b>"
        )

    async def _send_error(self, symbol, action, error):
        if self.telegram.chat_id:
            await self.telegram.send(
                f"🚨 <b>LIVE {html.escape(action.upper())} FAILED</b>\n"
                f"Token: <b>{html.escape(symbol)}</b>\n"
                f"Error: <code>{html.escape(str(error))[:700]}</code>\n\n"
                "No successful trade was recorded."
            )

    async def _buy(self, snap, result):
        balance = await self.market.solana_wallet_balance(
            self.cfg.wallet_public_address, self.cfg.solana_rpc_url
        )
        required = self.cfg.live_buy_sol + self.cfg.live_min_sol_reserve
        if balance < required:
            raise RuntimeError(
                f"Balance {balance:.6f} SOL is below buy+reserve requirement "
                f"{required:.6f} SOL."
            )

        requested = int(self.cfg.live_buy_sol * 1_000_000_000)
        swap = await self.market.jupiter_swap(
            SOL_MINT, snap.token_address, requested,
            self.cfg.wallet_public_address,
            self.cfg.solana_private_key_b58,
            self.cfg.live_slippage_bps,
        )
        r = swap["result"]
        token_raw = int(r.get("totalOutputAmount") or 0)
        sol_used = int(r.get("totalInputAmount") or requested)
        signature = str(r.get("signature") or "")
        if token_raw <= 0:
            raise RuntimeError("Jupiter reported zero output token amount.")

        self._open_position(snap, result, sol_used, token_raw, signature)
        await self._send_buy_message(snap, result, sol_used, token_raw, signature)

    async def _sell(self, snap, p, token_raw, reason):
        token_raw = min(
            max(int(token_raw), 0), int(p.get("remaining_token_raw") or 0)
        )
        if token_raw <= 0:
            return None
        swap = await self.market.jupiter_swap(
            snap.token_address, SOL_MINT, token_raw,
            self.cfg.wallet_public_address,
            self.cfg.solana_private_key_b58,
            self.cfg.live_slippage_bps,
        )
        r = swap["result"]
        sol_received = int(r.get("totalOutputAmount") or 0)
        signature = str(r.get("signature") or "")
        if sol_received <= 0:
            raise RuntimeError("Jupiter reported zero SOL output.")

        sold = self._record_sell(p, token_raw, sol_received, reason, signature)
        await self._send_sell_message(snap.token_symbol, sold)
        return sold

    async def manage(self, snap, result):
        p = self.get_open(snap.chain, snap.token_address)
        if p:
            self._update_mark(p, snap, result)
            if not self.execution_ready():
                return

            entry = float(p.get("entry_price_usd") or 0)
            if entry <= 0 or not snap.price_usd or snap.price_usd <= 0:
                return
            ret = ((float(snap.price_usd) / entry) - 1) * 100

            entry_liq = float(p.get("entry_liquidity") or 0)
            liq_drop = (
                entry_liq > 0
                and snap.liquidity_usd <=
                entry_liq * (1 - self.cfg.live_liquidity_drop_percent / 100)
            )

            exit_reason = None
            if ret <= -self.cfg.live_stop_loss_percent:
                exit_reason = f"STOP LOSS {ret:+.1f}%"
            elif liq_drop:
                exit_reason = f"LIQUIDITY DROP ≥{self.cfg.live_liquidity_drop_percent:.0f}%"
            elif result.score < self.cfg.live_exit_score:
                exit_reason = f"SCORE COLLAPSE {result.score}/100"

            try:
                current = self.get_open(snap.chain, snap.token_address)
                if not current:
                    return

                if exit_reason:
                    await self._sell(
                        snap, current, int(current["remaining_token_raw"]), exit_reason
                    )
                    return

                initial = int(current["initial_token_raw"] or 0)
                if initial <= 0:
                    return

                if ret >= self.cfg.live_tp2_percent and not int(current["tp2_done"]):
                    if not int(current["tp1_done"]):
                        amount = max(1, round(
                            initial * self.cfg.live_tp1_sell_percent / 100
                        ))
                        await self._sell(
                            snap, current, amount,
                            f"TAKE PROFIT 1 +{self.cfg.live_tp1_percent:.0f}%"
                        )
                        self._mark_tp(current["id"], 1)

                    current = self.get_open(snap.chain, snap.token_address)
                    if current:
                        amount = max(1, round(
                            initial * self.cfg.live_tp2_sell_percent / 100
                        ))
                        await self._sell(
                            snap, current, amount,
                            f"TAKE PROFIT 2 +{self.cfg.live_tp2_percent:.0f}%"
                        )
                        self._mark_tp(current["id"], 2)
                    return

                if ret >= self.cfg.live_tp1_percent and not int(current["tp1_done"]):
                    amount = max(1, round(
                        initial * self.cfg.live_tp1_sell_percent / 100
                    ))
                    await self._sell(
                        snap, current, amount,
                        f"TAKE PROFIT 1 +{self.cfg.live_tp1_percent:.0f}%"
                    )
                    self._mark_tp(current["id"], 1)
            except Exception as e:
                await self._send_error(snap.token_symbol, "sell", e)
            return

        if self._buy_allowed(snap, result):
            try:
                await self._buy(snap, result)
            except Exception as e:
                await self._send_error(snap.token_symbol, "buy", e)
