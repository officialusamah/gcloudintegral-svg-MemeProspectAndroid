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

    def connect(self):
        return sqlite3.connect(self.path)

    def save_snapshot(self, s: PairSnapshot, score: ScoreResult):
        with self.connect() as c:
            c.execute(
                """INSERT INTO snapshots
                   (ts,chain,token_address,token_name,token_symbol,market_cap,liquidity_usd,
                    volume_m5,buys_m5,sells_m5,price_change_m5,score,reasons,warnings)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    int(time.time()), s.chain, s.token_address, s.token_name, s.token_symbol,
                    s.market_cap, s.liquidity_usd, s.volume_m5, s.buys_m5, s.sells_m5,
                    s.price_change_m5, score.score, " | ".join(score.reasons), " | ".join(score.warnings)
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

    def can_alert(self, chain: str, address: str, score: int, cooldown_minutes: int) -> bool:
        now = int(time.time())
        with self.connect() as c:
            row = c.execute(
                "SELECT last_alert_ts,last_score FROM alerts WHERE chain=? AND token_address=?",
                (chain, address),
            ).fetchone()
        if not row:
            return True
        age = now - int(row[0])
        return age >= cooldown_minutes * 60 or score >= int(row[1]) + 5

    def mark_alert(self, chain: str, address: str, score: int):
        with self.connect() as c:
            c.execute(
                """INSERT INTO alerts(chain,token_address,last_alert_ts,last_score)
                   VALUES(?,?,?,?)
                   ON CONFLICT(chain,token_address)
                   DO UPDATE SET last_alert_ts=excluded.last_alert_ts,last_score=excluded.last_score""",
                (chain, address, int(time.time()), score),
            )

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
