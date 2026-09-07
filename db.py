from __future__ import annotations
import sqlite3, time
from pathlib import Path
from .models import PairSnapshot, ScoreResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  chain TEXT NOT NULL,
  token_address TEXT NOT NULL,
  token_name TEXT,
  token_symbol TEXT,
  market_cap REAL,
  liquidity_usd REAL,
  volume_m5 REAL,
  buys_m5 INTEGER,
  sells_m5 INTEGER,
  price_change_m5 REAL,
  price_usd REAL,
  score INTEGER,
  reasons TEXT,
  warnings TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_token
ON snapshots(chain, token_address, ts DESC);

CREATE TABLE IF NOT EXISTS alerts (
  chain TEXT NOT NULL,
  token_address TEXT NOT NULL,
  last_alert_ts INTEGER NOT NULL,
  last_score INTEGER NOT NULL,
  PRIMARY KEY(chain, token_address)
);

CREATE TABLE IF NOT EXISTS signal_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  chain TEXT NOT NULL,
  token_address TEXT NOT NULL,
  token_symbol TEXT,
  signal_type TEXT NOT NULL,
  score INTEGER NOT NULL,
  price_usd REAL,
  market_cap REAL,
  liquidity_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_signal_events_recent
ON signal_events(ts DESC);

CREATE TABLE IF NOT EXISTS paper_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chain TEXT NOT NULL,
  token_address TEXT NOT NULL,
  token_symbol TEXT,
  opened_ts INTEGER NOT NULL,
  closed_ts INTEGER,
  entry_price REAL NOT NULL,
  entry_score INTEGER NOT NULL,
  entry_market_cap REAL,
  entry_liquidity REAL,
  initial_usd REAL NOT NULL,
  remaining_percent REAL NOT NULL DEFAULT 100,
  realized_proceeds_usd REAL NOT NULL DEFAULT 0,
  realized_pnl_usd REAL NOT NULL DEFAULT 0,
  tp1_done INTEGER NOT NULL DEFAULT 0,
  tp2_done INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'OPEN',
  exit_reason TEXT,
  last_price REAL,
  last_score INTEGER,
  last_liquidity REAL,
  UNIQUE(chain, token_address, status)
);
CREATE INDEX IF NOT EXISTS idx_paper_positions_status
ON paper_positions(status, opened_ts DESC);

CREATE TABLE IF NOT EXISTS paper_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  position_id INTEGER NOT NULL,
  side TEXT NOT NULL,
  percent_of_original REAL NOT NULL,
  price_usd REAL NOT NULL,
  notional_cost_usd REAL NOT NULL,
  proceeds_usd REAL NOT NULL,
  pnl_usd REAL NOT NULL,
  reason TEXT,
  FOREIGN KEY(position_id) REFERENCES paper_positions(id)
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_position
ON paper_trades(position_id, ts);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as c:
            c.executescript(SCHEMA)
            cols = {row[1] for row in c.execute("PRAGMA table_info(snapshots)").fetchall()}
            if "price_usd" not in cols:
                c.execute("ALTER TABLE snapshots ADD COLUMN price_usd REAL")

    def connect(self):
        return sqlite3.connect(self.path)

    def save_snapshot(self, s: PairSnapshot, score: ScoreResult):
        with self.connect() as c:
            c.execute(
                """INSERT INTO snapshots
                   (ts,chain,token_address,token_name,token_symbol,market_cap,liquidity_usd,
                    volume_m5,buys_m5,sells_m5,price_change_m5,price_usd,score,reasons,warnings)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    int(time.time()), s.chain, s.token_address, s.token_name, s.token_symbol,
                    s.market_cap, s.liquidity_usd, s.volume_m5, s.buys_m5, s.sells_m5,
                    s.price_change_m5, s.price_usd, score.score,
                    " | ".join(score.reasons), " | ".join(score.warnings)
                ),
            )

    def previous_snapshot(self, chain: str, address: str):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                """SELECT * FROM snapshots WHERE chain=? AND token_address=?
                   ORDER BY ts DESC LIMIT 1""",
                (chain, address),
            ).fetchone()
            return dict(row) if row else None

    def alert_state(self, chain: str, address: str):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT last_alert_ts,last_score FROM alerts WHERE chain=? AND token_address=?",
                (chain, address),
            ).fetchone()
            return dict(row) if row else None

    def can_alert(
        self, chain: str, address: str, score: int, cooldown_minutes: int,
        strong_threshold: int = 85, rapid_acceleration: bool = False
    ) -> bool:
        now = int(time.time())
        row = self.alert_state(chain, address)
        if not row:
            return True
        last_ts = int(row["last_alert_ts"])
        last_score = int(row["last_score"])
        age = now - last_ts
        if last_score < strong_threshold <= score:
            return True
        if rapid_acceleration and age >= 180:
            return True
        return age >= cooldown_minutes * 60 or score >= last_score + 5

    def mark_alert(self, chain: str, address: str, score: int):
        with self.connect() as c:
            c.execute(
                """INSERT INTO alerts(chain,token_address,last_alert_ts,last_score)
                   VALUES(?,?,?,?)
                   ON CONFLICT(chain,token_address)
                   DO UPDATE SET last_alert_ts=excluded.last_alert_ts,last_score=excluded.last_score""",
                (chain, address, int(time.time()), score),
            )

    def record_signal(self, s: PairSnapshot, score: int, signal_type: str):
        with self.connect() as c:
            c.execute(
                """INSERT INTO signal_events
                   (ts,chain,token_address,token_symbol,signal_type,score,price_usd,market_cap,liquidity_usd)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    int(time.time()), s.chain, s.token_address, s.token_symbol,
                    signal_type, int(score), float(s.price_usd or 0),
                    float(s.market_cap or 0), float(s.liquidity_usd or 0),
                ),
            )

    def recent_performance(self, limit: int = 5):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            events = c.execute(
                """SELECT * FROM signal_events
                   WHERE price_usd IS NOT NULL AND price_usd > 0
                   ORDER BY ts DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            out = []
            for event in events:
                e = dict(event)
                latest = c.execute(
                    """SELECT price_usd,score,ts FROM snapshots
                       WHERE chain=? AND token_address=? AND price_usd IS NOT NULL AND price_usd > 0
                       ORDER BY ts DESC LIMIT 1""",
                    (e["chain"], e["token_address"]),
                ).fetchone()
                best = c.execute(
                    """SELECT MAX(price_usd) FROM snapshots
                       WHERE chain=? AND token_address=? AND ts>=?
                       AND price_usd IS NOT NULL AND price_usd > 0""",
                    (e["chain"], e["token_address"], e["ts"]),
                ).fetchone()
                entry = float(e["price_usd"] or 0)
                current = float(latest["price_usd"]) if latest else 0.0
                best_price = float(best[0] or 0) if best else 0.0
                e["current_price"] = current
                e["current_score"] = int(latest["score"]) if latest else None
                e["return_pct"] = ((current / entry) - 1) * 100 if entry and current else None
                e["best_return_pct"] = ((best_price / entry) - 1) * 100 if entry and best_price else None
                out.append(e)
            return out

    # ---------- PAPER TRADER ----------
    def open_paper_position(self, s: PairSnapshot, score: int, initial_usd: float):
        if not s.price_usd or s.price_usd <= 0:
            return None
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            existing = c.execute(
                """SELECT * FROM paper_positions
                   WHERE chain=? AND token_address=? AND status='OPEN' LIMIT 1""",
                (s.chain, s.token_address),
            ).fetchone()
            if existing:
                return dict(existing)

            cur = c.execute(
                """INSERT INTO paper_positions
                   (chain,token_address,token_symbol,opened_ts,entry_price,entry_score,
                    entry_market_cap,entry_liquidity,initial_usd,remaining_percent,
                    last_price,last_score,last_liquidity)
                   VALUES(?,?,?,?,?,?,?,?,?,100,?,?,?)""",
                (
                    s.chain, s.token_address, s.token_symbol, int(time.time()),
                    float(s.price_usd), int(score), float(s.market_cap or 0),
                    float(s.liquidity_usd or 0), float(initial_usd),
                    float(s.price_usd), int(score), float(s.liquidity_usd or 0),
                ),
            )
            position_id = cur.lastrowid
            c.execute(
                """INSERT INTO paper_trades
                   (ts,position_id,side,percent_of_original,price_usd,
                    notional_cost_usd,proceeds_usd,pnl_usd,reason)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    int(time.time()), position_id, "BUY", 100.0, float(s.price_usd),
                    float(initial_usd), float(initial_usd), 0.0, "AUTO PAPER BUY",
                ),
            )
            row = c.execute("SELECT * FROM paper_positions WHERE id=?", (position_id,)).fetchone()
            return dict(row)

    def get_open_paper_position(self, chain: str, address: str):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                """SELECT * FROM paper_positions
                   WHERE chain=? AND token_address=? AND status='OPEN' LIMIT 1""",
                (chain, address),
            ).fetchone()
            return dict(row) if row else None

    def open_paper_positions(self):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                """SELECT * FROM paper_positions
                   WHERE status='OPEN' ORDER BY opened_ts DESC"""
            ).fetchall()
            return [dict(x) for x in rows]

    def open_paper_count(self) -> int:
        with self.connect() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'"
            ).fetchone()
            return int(row[0] or 0)

    def update_paper_mark(self, position_id: int, price: float, score: int, liquidity: float):
        with self.connect() as c:
            c.execute(
                """UPDATE paper_positions
                   SET last_price=?,last_score=?,last_liquidity=?
                   WHERE id=? AND status='OPEN'""",
                (float(price or 0), int(score), float(liquidity or 0), int(position_id)),
            )

    def paper_sell(self, position_id: int, price: float, percent_of_original: float, reason: str):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            p = c.execute(
                "SELECT * FROM paper_positions WHERE id=? AND status='OPEN'",
                (int(position_id),),
            ).fetchone()
            if not p:
                return None

            p = dict(p)
            remaining = float(p["remaining_percent"] or 0)
            sell_pct = min(max(float(percent_of_original), 0.0), remaining)
            if sell_pct <= 0 or not price or price <= 0:
                return None

            initial_usd = float(p["initial_usd"])
            entry_price = float(p["entry_price"])
            cost_slice = initial_usd * (sell_pct / 100.0)
            proceeds = cost_slice * (float(price) / entry_price)
            pnl = proceeds - cost_slice

            new_remaining = max(0.0, remaining - sell_pct)
            new_proceeds = float(p["realized_proceeds_usd"] or 0) + proceeds
            new_pnl = float(p["realized_pnl_usd"] or 0) + pnl
            closed = new_remaining <= 0.0001

            c.execute(
                """INSERT INTO paper_trades
                   (ts,position_id,side,percent_of_original,price_usd,
                    notional_cost_usd,proceeds_usd,pnl_usd,reason)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    int(time.time()), int(position_id), "SELL", sell_pct, float(price),
                    cost_slice, proceeds, pnl, reason,
                ),
            )
            c.execute(
                """UPDATE paper_positions
                   SET remaining_percent=?,realized_proceeds_usd=?,realized_pnl_usd=?,
                       status=?,closed_ts=?,exit_reason=?,last_price=?
                   WHERE id=?""",
                (
                    new_remaining, new_proceeds, new_pnl,
                    "CLOSED" if closed else "OPEN",
                    int(time.time()) if closed else None,
                    reason if closed else p.get("exit_reason"),
                    float(price), int(position_id),
                ),
            )
            row = c.execute("SELECT * FROM paper_positions WHERE id=?", (int(position_id),)).fetchone()
            result = dict(row)
            result["sell_percent"] = sell_pct
            result["sell_proceeds"] = proceeds
            result["sell_pnl"] = pnl
            result["sell_reason"] = reason
            return result

    def mark_paper_tp(self, position_id: int, which: int):
        col = "tp1_done" if which == 1 else "tp2_done"
        with self.connect() as c:
            c.execute(f"UPDATE paper_positions SET {col}=1 WHERE id=?", (int(position_id),))

    def paper_summary(self):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            open_rows = c.execute(
                "SELECT * FROM paper_positions WHERE status='OPEN' ORDER BY opened_ts DESC"
            ).fetchall()
            closed_rows = c.execute(
                "SELECT * FROM paper_positions WHERE status='CLOSED' ORDER BY closed_ts DESC"
            ).fetchall()

            open_positions = [dict(x) for x in open_rows]
            closed_positions = [dict(x) for x in closed_rows]

            unrealized = 0.0
            for p in open_positions:
                entry = float(p["entry_price"] or 0)
                last = float(p["last_price"] or 0)
                remaining_pct = float(p["remaining_percent"] or 0)
                initial = float(p["initial_usd"] or 0)
                if entry > 0 and last > 0:
                    cost = initial * remaining_pct / 100.0
                    value = cost * last / entry
                    unrealized += value - cost

            realized = sum(float(p["realized_pnl_usd"] or 0) for p in open_positions + closed_positions)
            return {
                "open": open_positions,
                "closed_count": len(closed_positions),
                "realized_pnl_usd": realized,
                "unrealized_pnl_usd": unrealized,
            }

    def set_setting(self, key: str, value: str):
        with self.connect() as c:
            c.execute(
                """INSERT INTO settings(key,value) VALUES(?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value),
            )

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def latest_top(self, limit: int = 5):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                """SELECT s.* FROM snapshots s
                   INNER JOIN (
                     SELECT chain,token_address,MAX(ts) AS max_ts
                     FROM snapshots GROUP BY chain,token_address
                   ) x ON x.chain=s.chain AND x.token_address=s.token_address AND x.max_ts=s.ts
                   ORDER BY score DESC, ts DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(x) for x in rows]
