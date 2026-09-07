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

    def connect(self):
        return sqlite3.connect(self.store.path)

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

    def _open_signal(self, snap, result):
        with self.connect() as c:
            cur = c.execute(
                """INSERT INTO live_signal_positions
                   (chain,token_address,token_symbol,opened_ts,entry_price_usd,
                    entry_score,entry_liquidity,entry_market_cap,last_price_usd,
                    last_score,status)
                   VALUES(?,?,?,?,?,?,?,?,?,?, 'OPEN')""",
                (
                    snap.chain, snap.token_address, snap.token_symbol,
                    int(time.time()), float(snap.price_usd), int(result.score),
                    float(snap.liquidity_usd or 0), float(snap.market_cap or 0),
                    float(snap.price_usd), int(result.score),
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

    def _jupiter_url(self, input_asset: str, output_asset: str) -> str:
        return f"https://jup.ag/swap/{input_asset}-{output_asset}"

    def _solflare_browse_url(self, target_url: str) -> str:
        encoded_target = quote(target_url, safe="")
        ref = quote("https://t.me/Memeprospects_bot", safe="")
        return f"https://solflare.com/ul/v1/browse/{encoded_target}?ref={ref}"

    def _buy_buttons(self, token_mint: str):
        jup = self._jupiter_url("SOL", token_mint)
        return [
            [{"text": f"🟢 BUY {self.cfg.live_buy_sol:.3f} SOL — SOLFLARE",
              "url": self._solflare_browse_url(jup)}],
            [{"text": "🪐 Open Jupiter", "url": jup}],
        ]

    def _sell_buttons(self, token_mint: str):
        jup = self._jupiter_url(token_mint, "SOL")
        return [
            [{"text": "🔴 SELL ALL — SOLFLARE",
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
            f"🟢 <b>LIVE BUY SIGNAL — CONFIRM IN WALLET</b>\n\n"
            f"<b>{html.escape(snap.token_name)} ({html.escape(snap.token_symbol)})</b>\n"
            f"Score: <b>{result.score}/100</b>\n"
            f"Signal entry: <b>${snap.price_usd:.10f}</b>\n"
            f"Liquidity: <b>${snap.liquidity_usd:,.0f}</b>\n"
            f"Market cap: <b>${snap.market_cap:,.0f}</b>\n\n"
            f"Planned size: <b>{self.cfg.live_buy_sol:.3f} SOL</b>\n"
            f"Target: <b>+{self.cfg.live_signal_take_profit_percent:.0f}% → SELL ALL</b>\n"
            f"Stop signal: <b>-{self.cfg.live_signal_stop_loss_percent:.0f}%</b>\n\n"
            f"Tap the button below. Jupiter opens with the SOL/token pair selected. "
            f"Verify <b>{self.cfg.live_buy_sol:.3f} SOL</b>, review price impact/slippage, "
            f"then approve the swap in Solflare.\n\n"
            "🔐 The bot never receives your private key and cannot approve the transaction for you.",
            buttons=self._buy_buttons(snap.token_address),
        )

    async def _send_sell_signal(self, snap, ret: float, reason: str):
        if not self.telegram.chat_id:
            return
        await self.telegram.send(
            f"💰 <b>LIVE SELL ALL SIGNAL — CONFIRM IN WALLET</b>\n\n"
            f"<b>{html.escape(snap.token_symbol)}</b>\n"
            f"Signal return: <b>{ret:+.1f}%</b>\n"
            f"Reason: <b>{html.escape(reason)}</b>\n\n"
            "Tap <b>SELL ALL</b>, choose <b>MAX</b> in Jupiter, review the quote, "
            "and approve in Solflare.\n\n"
            "🔎 After this exit signal, the bot immediately scans for a different prospect.\n"
            "🔐 No transaction is submitted without your wallet approval.",
            buttons=self._sell_buttons(snap.token_address),
        )

    async def manage(self, snap, result) -> bool:
        """
        Returns True when a signal position closes, so Scanner can rescan immediately.
        """
        p = self.get_open(snap.chain, snap.token_address)

        if p:
            self._update_mark(p["id"], snap, result)
            entry = float(p.get("entry_price_usd") or 0)
            if entry <= 0 or not snap.price_usd or snap.price_usd <= 0:
                return False

            ret = ((float(snap.price_usd) / entry) - 1.0) * 100

            if ret >= self.cfg.live_signal_take_profit_percent:
                reason = (
                    f"TAKE PROFIT +{self.cfg.live_signal_take_profit_percent:.0f}% "
                    "→ SELL ALL"
                )
                self._close_signal(p["id"], reason)
                await self._send_sell_signal(snap, ret, reason)
                return True

            if ret <= -self.cfg.live_signal_stop_loss_percent:
                reason = (
                    f"STOP SIGNAL -{self.cfg.live_signal_stop_loss_percent:.0f}% "
                    "→ EXIT ALL"
                )
                self._close_signal(p["id"], reason)
                await self._send_sell_signal(snap, ret, reason)
                return True

            if result.hard_block or result.score < self.cfg.live_exit_score:
                reason = f"RISK / SCORE COLLAPSE {result.score}/100 → EXIT ALL"
                self._close_signal(p["id"], reason)
                await self._send_sell_signal(snap, ret, reason)
                return True

            return False

        if self._signal_buy_allowed(snap, result):
            self._open_signal(snap, result)
            await self._send_buy_signal(snap, result)

        return False
