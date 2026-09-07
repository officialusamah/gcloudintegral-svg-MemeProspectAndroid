from __future__ import annotations
import secrets, sqlite3, time
from pathlib import Path

ACCOUNT_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_users (
  chat_id TEXT PRIMARY KEY,
  username TEXT,
  first_name TEXT,
  wallet_address TEXT NOT NULL DEFAULT '',
  created_ts INTEGER NOT NULL,
  last_seen_ts INTEGER NOT NULL,
  subscription_until INTEGER NOT NULL DEFAULT 0,
  live_enabled INTEGER NOT NULL DEFAULT 1,
  is_admin INTEGER NOT NULL DEFAULT 0,
  referral_code TEXT UNIQUE,
  referred_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_bot_users_subscription
ON bot_users(subscription_until);

CREATE TABLE IF NOT EXISTS subscription_payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  chat_id TEXT NOT NULL,
  amount_ngn REAL NOT NULL DEFAULT 0,
  days INTEGER NOT NULL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_subscription_payments_ts
ON subscription_payments(ts DESC);
"""

class Accounts:
    def __init__(self, db_path: str, configured_admin_chat_id: str = "", legacy_wallet: str = ""):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        with self.connect() as c:
            c.executescript(ACCOUNT_SCHEMA)

        admin = str(configured_admin_chat_id or "").strip()

        # Migrate the old single-chat pairing when available.
        if not admin:
            try:
                with self.connect() as c:
                    row = c.execute(
                        "SELECT value FROM settings WHERE key='telegram_chat_id'"
                    ).fetchone()
                if row and row[0]:
                    admin = str(row[0])
            except Exception:
                pass

        if admin:
            self.register(admin, "", "Owner", make_admin=True)
            if legacy_wallet and not self.get(admin).get("wallet_address"):
                self.set_wallet(admin, legacy_wallet)

    def connect(self):
        return sqlite3.connect(self.path)

    def _new_referral_code(self) -> str:
        for _ in range(20):
            code = "MP" + secrets.token_hex(4).upper()
            with self.connect() as c:
                exists = c.execute(
                    "SELECT 1 FROM bot_users WHERE referral_code=? LIMIT 1", (code,)
                ).fetchone()
            if not exists:
                return code
        return "MP" + secrets.token_hex(6).upper()

    def register(
        self,
        chat_id: str,
        username: str = "",
        first_name: str = "",
        referral_code: str = "",
        make_admin: bool = False,
    ) -> dict:
        chat_id = str(chat_id)
        now = int(time.time())
        existing = self.get(chat_id)

        # If nobody is admin yet, the first registered chat becomes owner/admin.
        if not existing and not self.admin_chat_id():
            make_admin = True

        referred_by = ""
        if referral_code:
            referrer = self.by_referral_code(referral_code)
            if referrer and str(referrer["chat_id"]) != chat_id:
                referred_by = str(referrer["chat_id"])

        with self.connect() as c:
            if existing:
                c.execute(
                    """UPDATE bot_users
                       SET username=?,first_name=?,last_seen_ts=?,
                           is_admin=CASE WHEN ?=1 THEN 1 ELSE is_admin END
                       WHERE chat_id=?""",
                    (
                        str(username or ""),
                        str(first_name or ""),
                        now,
                        1 if make_admin else 0,
                        chat_id,
                    ),
                )
            else:
                c.execute(
                    """INSERT INTO bot_users
                       (chat_id,username,first_name,wallet_address,created_ts,last_seen_ts,
                        subscription_until,live_enabled,is_admin,referral_code,referred_by)
                       VALUES(?,?,?,'',?,?,0,1,?,?,?)""",
                    (
                        chat_id,
                        str(username or ""),
                        str(first_name or ""),
                        now,
                        now,
                        1 if make_admin else 0,
                        self._new_referral_code(),
                        referred_by,
                    ),
                )
        return self.get(chat_id)

    def get(self, chat_id: str) -> dict | None:
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT * FROM bot_users WHERE chat_id=?",
                (str(chat_id),),
            ).fetchone()
            return dict(row) if row else None

    def by_referral_code(self, code: str) -> dict | None:
        code = str(code or "").strip().upper()
        if not code:
            return None
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT * FROM bot_users WHERE referral_code=? LIMIT 1",
                (code,),
            ).fetchone()
            return dict(row) if row else None

    def admin_chat_id(self) -> str:
        with self.connect() as c:
            row = c.execute(
                "SELECT chat_id FROM bot_users WHERE is_admin=1 ORDER BY created_ts LIMIT 1"
            ).fetchone()
        return str(row[0]) if row else ""

    def is_admin(self, chat_id: str) -> bool:
        u = self.get(chat_id)
        return bool(u and int(u.get("is_admin") or 0))

    def is_active(self, chat_id: str) -> bool:
        u = self.get(chat_id)
        if not u:
            return False
        if int(u.get("is_admin") or 0):
            return True
        return int(u.get("subscription_until") or 0) > int(time.time())

    def subscription_days_left(self, chat_id: str) -> int:
        u = self.get(chat_id)
        if not u:
            return 0
        if int(u.get("is_admin") or 0):
            return 999999
        remaining = int(u.get("subscription_until") or 0) - int(time.time())
        return max(0, (remaining + 86399) // 86400)

    def set_wallet(self, chat_id: str, address: str):
        with self.connect() as c:
            c.execute(
                "UPDATE bot_users SET wallet_address=?,last_seen_ts=? WHERE chat_id=?",
                (str(address or "").strip(), int(time.time()), str(chat_id)),
            )

    def set_live_enabled(self, chat_id: str, enabled: bool):
        with self.connect() as c:
            c.execute(
                "UPDATE bot_users SET live_enabled=?,last_seen_ts=? WHERE chat_id=?",
                (1 if enabled else 0, int(time.time()), str(chat_id)),
            )

    def activate(self, chat_id: str, days: int, amount_ngn: float = 0, note: str = ""):
        days = max(1, int(days))
        now = int(time.time())
        u = self.get(chat_id)
        if not u:
            self.register(chat_id)
            u = self.get(chat_id)

        current_until = int(u.get("subscription_until") or 0)
        start = max(now, current_until)
        until = start + days * 86400
        with self.connect() as c:
            c.execute(
                "UPDATE bot_users SET subscription_until=?,live_enabled=1 WHERE chat_id=?",
                (until, str(chat_id)),
            )
            c.execute(
                """INSERT INTO subscription_payments(ts,chat_id,amount_ngn,days,note)
                   VALUES(?,?,?,?,?)""",
                (now, str(chat_id), float(amount_ngn or 0), days, str(note or "")),
            )
        return self.get(chat_id)

    def deactivate(self, chat_id: str):
        with self.connect() as c:
            c.execute(
                "UPDATE bot_users SET subscription_until=0,live_enabled=0 WHERE chat_id=?",
                (str(chat_id),),
            )

    def active_users(self) -> list[dict]:
        now = int(time.time())
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                """SELECT * FROM bot_users
                   WHERE is_admin=1 OR subscription_until>?
                   ORDER BY is_admin DESC,created_ts ASC""",
                (now,),
            ).fetchall()
            return [dict(x) for x in rows]

    def trade_users(self) -> list[dict]:
        return [
            u for u in self.active_users()
            if int(u.get("live_enabled") or 0) and str(u.get("wallet_address") or "")
        ]

    def all_users(self, limit: int = 50) -> list[dict]:
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                """SELECT * FROM bot_users
                   ORDER BY is_admin DESC,created_ts DESC LIMIT ?""",
                (int(limit),),
            ).fetchall()
            return [dict(x) for x in rows]

    def referral_count(self, chat_id: str) -> int:
        with self.connect() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM bot_users WHERE referred_by=?",
                (str(chat_id),),
            ).fetchone()
        return int(row[0] or 0)

    def stats(self) -> dict:
        now = int(time.time())
        with self.connect() as c:
            total = int(c.execute("SELECT COUNT(*) FROM bot_users").fetchone()[0] or 0)
            active = int(c.execute(
                "SELECT COUNT(*) FROM bot_users WHERE is_admin=1 OR subscription_until>?",
                (now,),
            ).fetchone()[0] or 0)
            wallets = int(c.execute(
                "SELECT COUNT(*) FROM bot_users WHERE wallet_address<>''"
            ).fetchone()[0] or 0)
            revenue = float(c.execute(
                "SELECT COALESCE(SUM(amount_ngn),0) FROM subscription_payments"
            ).fetchone()[0] or 0)
        return {
            "total_users": total,
            "active_users": active,
            "wallets": wallets,
            "recorded_revenue_ngn": revenue,
        }
