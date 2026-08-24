"""Users, API keys, and quotas for the public API.

Deliberately small: SQLite, one table. A user signs up (email + password),
gets an API key, and is metered by a daily quota. A separate global SMTP
counter caps *total* probing across everyone -- the single most important
guard, because it protects the server IP no matter how many users pile in.
"""

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT UNIQUE NOT NULL,
    pw_hash      TEXT NOT NULL,
    pw_salt      TEXT NOT NULL,
    api_key      TEXT UNIQUE NOT NULL,
    plan         TEXT NOT NULL DEFAULT 'free',
    daily_quota  INTEGER NOT NULL DEFAULT 100,
    used_today   INTEGER NOT NULL DEFAULT 0,
    quota_date   TEXT NOT NULL DEFAULT '',
    total_checks INTEGER NOT NULL DEFAULT 0,
    disabled     INTEGER NOT NULL DEFAULT 0,
    is_admin     INTEGER NOT NULL DEFAULT 0,
    verified     INTEGER NOT NULL DEFAULT 0,
    email_token  TEXT DEFAULT '',
    reset_token  TEXT DEFAULT '',
    reset_at     TEXT DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_apikey ON users(api_key);

-- Rolling counters (global SMTP cap, etc.), keyed by name.
CREATE TABLE IF NOT EXISTS counters (
    name        TEXT PRIMARY KEY,
    window_start TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0
);

-- Every check/find a user runs, for the admin's audit view.
CREATE TABLE IF NOT EXISTS queries (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    kind     TEXT NOT NULL,          -- verify | find | bulk
    query    TEXT NOT NULL,          -- the email or "name @ domain"
    result   TEXT NOT NULL,
    via      TEXT NOT NULL DEFAULT 'web',   -- web | api
    at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queries_user ON queries(user_id, id);
"""

FREE_DAILY_QUOTA = 100

OTP_TTL_SECONDS = 15 * 60     # a code is valid for 15 minutes
OTP_MAX_TRIES = 5             # wrong guesses before a fresh code is required


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


def _hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 120_000).hex()


def _otp() -> str:
    """A 6-digit numeric code, uniform over 000000..999999."""
    return "%06d" % secrets.randbelow(1_000_000)


class Users:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # Migrate older DBs that predate newer columns.
            cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
            for name, ddl in (
                    ("is_admin", "INTEGER NOT NULL DEFAULT 0"),
                    ("verified", "INTEGER NOT NULL DEFAULT 0"),
                    ("email_token", "TEXT DEFAULT ''"),
                    ("email_token_at", "TEXT DEFAULT ''"),
                    ("otp_tries", "INTEGER NOT NULL DEFAULT 0"),
                    ("reset_token", "TEXT DEFAULT ''"),
                    ("reset_at", "TEXT DEFAULT ''")):
                if name not in cols:
                    conn.execute("ALTER TABLE users ADD COLUMN %s %s" % (name, ddl))

            # One-time: when email verification becomes mandatory, grandfather
            # every account that already exists so the new gate never locks out
            # a user who signed up before it. New signups start unverified.
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            if ver < 1:
                conn.execute("UPDATE users SET verified = 1")
                conn.execute("PRAGMA user_version = 1")

    def seed_admin(self, email: str, password: str) -> None:
        """Ensure an admin account exists (owner). Idempotent."""
        email = email.strip().lower()
        existing = self.by_email(email)
        with self._conn() as conn:
            if existing is None:
                salt = secrets.token_hex(16)
                conn.execute(
                    """INSERT INTO users (email, pw_hash, pw_salt, api_key,
                        daily_quota, quota_date, is_admin, verified, created_at)
                       VALUES (?,?,?,?,?,?,1,1,?)""",
                    (email, _hash_pw(password, salt), salt,
                     "ev_" + secrets.token_urlsafe(32), 1_000_000, _today(),
                     _now().isoformat(timespec="seconds")))
            else:
                conn.execute(
                    "UPDATE users SET is_admin = 1, verified = 1 WHERE email = ?",
                    (email,))

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # -- accounts ---------------------------------------------------------

    def create(self, email: str, password: str) -> dict:
        email = email.strip().lower()
        salt = secrets.token_hex(16)
        api_key = "ev_" + secrets.token_urlsafe(32)
        code = _otp()
        with self._conn() as conn:
            try:
                conn.execute(
                    """INSERT INTO users
                       (email, pw_hash, pw_salt, api_key, daily_quota, quota_date,
                        email_token, email_token_at, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (email, _hash_pw(password, salt), salt, api_key,
                     FREE_DAILY_QUOTA, _today(), code,
                     _now().isoformat(timespec="seconds"),
                     _now().isoformat(timespec="seconds")))
            except sqlite3.IntegrityError:
                raise ValueError("an account with that email already exists")
        return self.by_email(email)

    def issue_otp(self, user_id: int) -> str:
        """Generate a fresh code, reset the attempt counter, and return it."""
        code = _otp()
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET email_token = ?, email_token_at = ?, "
                "otp_tries = 0 WHERE id = ?",
                (code, _now().isoformat(timespec="seconds"), user_id))
        return code

    def check_otp(self, user_id: int, code: str):
        """Validate a submitted code. Returns (ok: bool, reason: str).

        reason is one of: ok, already, expired, locked, bad -- so the UI can
        show a helpful message. A correct code marks the account verified and
        clears the code; a wrong one burns one of OTP_MAX_TRIES attempts.
        """
        code = (code or "").strip()
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?",
                               (user_id,)).fetchone()
            if row is None:
                return False, "bad"
            if row["verified"]:
                return True, "already"
            stored = row["email_token"] or ""
            if not stored:
                return False, "expired"
            # Expiry.
            try:
                issued = datetime.fromisoformat(row["email_token_at"])
                if issued.tzinfo is None:
                    issued = issued.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                issued = None
            if issued is None or (_now() - issued).total_seconds() > OTP_TTL_SECONDS:
                return False, "expired"
            # Too many wrong guesses -- force a resend.
            if row["otp_tries"] >= OTP_MAX_TRIES:
                return False, "locked"
            # Constant-time compare so timing can't leak the code.
            if hmac.compare_digest(stored, code):
                conn.execute("UPDATE users SET verified = 1, email_token = '', "
                             "email_token_at = '', otp_tries = 0 WHERE id = ?",
                             (user_id,))
                return True, "ok"
            conn.execute("UPDATE users SET otp_tries = otp_tries + 1 WHERE id = ?",
                         (user_id,))
            return False, "bad"

    # -- password reset ---------------------------------------------------

    def start_reset(self, email: str) -> Optional[str]:
        """Issue a reset token. Returns None if no such account (caller should
        still show the same 'check your email' message -- never leak which
        addresses are registered)."""
        user = self.by_email(email)
        if user is None:
            return None
        token = secrets.token_urlsafe(24)
        with self._conn() as conn:
            conn.execute("UPDATE users SET reset_token = ?, reset_at = ? "
                         "WHERE id = ?",
                         (token, _now().isoformat(timespec="seconds"), user["id"]))
        return token

    def reset_password(self, token: str, new_password: str) -> bool:
        """Consume a reset token (valid 1 hour) and set the new password."""
        if not token:
            return False
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE reset_token = ?",
                               (token,)).fetchone()
            if row is None:
                return False
            try:
                issued = datetime.fromisoformat(row["reset_at"])
                if issued.tzinfo is None:
                    issued = issued.replace(tzinfo=timezone.utc)
                if (_now() - issued).total_seconds() > 3600:
                    return False
            except (ValueError, TypeError):
                return False
            salt = secrets.token_hex(16)
            conn.execute(
                "UPDATE users SET pw_hash = ?, pw_salt = ?, reset_token = '', "
                "reset_at = '' WHERE id = ?",
                (_hash_pw(new_password, salt), salt, row["id"]))
        return True

    def set_plan(self, user_id: int, plan: str, quota: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE users SET plan = ?, daily_quota = ? WHERE id = ?",
                         (plan, max(0, int(quota)), user_id))

    def daily_usage(self, days: int = 14, user_id: Optional[int] = None):
        """Return [(YYYY-MM-DD, count)] for the last `days`, oldest first."""
        where = "WHERE at >= ?"
        since = (_now() - __import__("datetime").timedelta(days=days - 1)) \
            .strftime("%Y-%m-%d")
        args = [since]
        if user_id is not None:
            where += " AND user_id = ?"
            args.append(user_id)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT substr(at,1,10) d, COUNT(*) n FROM queries "
                + where + " GROUP BY d", args).fetchall()
        counts = {r["d"]: r["n"] for r in rows}
        out = []
        base = _now()
        for i in range(days - 1, -1, -1):
            day = (base - __import__("datetime").timedelta(days=i)).strftime("%Y-%m-%d")
            out.append((day, counts.get(day, 0)))
        return out

    def by_email(self, email: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?",
                               (email.strip().lower(),)).fetchone()
        return dict(row) if row else None

    def by_api_key(self, api_key: str) -> Optional[dict]:
        if not api_key:
            return None
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE api_key = ?",
                               (api_key.strip(),)).fetchone()
        return dict(row) if row else None

    def check_password(self, email: str, password: str) -> Optional[dict]:
        user = self.by_email(email)
        if user is None:
            return None
        expect = user["pw_hash"]
        got = _hash_pw(password, user["pw_salt"])
        return user if hmac.compare_digest(expect, got) else None

    def rotate_key(self, user_id: int) -> str:
        api_key = "ev_" + secrets.token_urlsafe(32)
        with self._conn() as conn:
            conn.execute("UPDATE users SET api_key = ? WHERE id = ?",
                         (api_key, user_id))
        return api_key

    # -- quota ------------------------------------------------------------

    def consume(self, user_id: int, n: int = 1) -> tuple:
        """Try to spend n checks from the user's daily quota.
        Returns (ok, remaining). Resets the counter on a new day."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?",
                               (user_id,)).fetchone()
            if row is None:
                return False, 0
            if row["disabled"]:
                return False, 0
            used = row["used_today"]
            if row["quota_date"] != _today():
                used = 0
            if used + n > row["daily_quota"]:
                return False, max(0, row["daily_quota"] - used)
            conn.execute(
                "UPDATE users SET used_today = ?, quota_date = ?, "
                "total_checks = total_checks + ? WHERE id = ?",
                (used + n, _today(), n, user_id))
            return True, row["daily_quota"] - (used + n)

    # -- admin ------------------------------------------------------------

    def list_users(self):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY created_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["used_today"] = d["used_today"] if d["quota_date"] == _today() else 0
            out.append(d)
        return out

    def set_quota(self, user_id: int, quota: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE users SET daily_quota = ? WHERE id = ?",
                         (max(0, int(quota)), user_id))

    def set_disabled(self, user_id: int, disabled: bool) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE users SET disabled = ? WHERE id = ?",
                         (1 if disabled else 0, user_id))

    def set_verified(self, user_id: int) -> None:
        """Admin escape hatch: confirm an account by hand (e.g. if the
        verification email never arrived)."""
        with self._conn() as conn:
            conn.execute("UPDATE users SET verified = 1, email_token = '' "
                         "WHERE id = ?", (user_id,))

    def log_query(self, user_id: int, kind: str, query: str, result: str,
                  via: str = "web") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO queries (user_id, kind, query, result, via, at) "
                "VALUES (?,?,?,?,?,?)",
                (user_id, kind, query[:300], result[:60], via,
                 _now().isoformat(timespec="seconds")))
            # Keep the log bounded -- only the most recent 20k rows.
            conn.execute("DELETE FROM queries WHERE id < "
                         "(SELECT MAX(id) - 20000 FROM queries)")

    def recent_queries(self, limit: int = 200, user_id: Optional[int] = None):
        sql = ("SELECT q.*, u.email FROM queries q JOIN users u ON u.id = q.user_id")
        args = ()
        if user_id is not None:
            sql += " WHERE q.user_id = ?"
            args = (user_id,)
        sql += " ORDER BY q.id DESC LIMIT ?"
        args = args + (limit,)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def admin_stats(self) -> dict:
        with self._conn() as conn:
            users_n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            checks = conn.execute("SELECT COALESCE(SUM(total_checks),0) "
                                  "FROM users").fetchone()[0]
            today = conn.execute(
                "SELECT COUNT(*) FROM queries WHERE at >= ?",
                (_today(),)).fetchone()[0]
        return {"users": users_n, "total_checks": checks, "today": today}

    # -- global rolling counter (e.g. server-wide SMTP cap) ---------------

    def bump_counter(self, name: str, limit: int, window_seconds: int,
                     n: int = 1) -> bool:
        """Increment a shared rolling counter; return False if it would exceed
        `limit` within the window. Used to cap total SMTP probes across ALL
        users so the server IP can't be burst-flagged."""
        now = _now()
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM counters WHERE name = ?",
                               (name,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO counters (name, window_start, count) "
                             "VALUES (?,?,?)", (name, now.isoformat(), n))
                return n <= limit
            start = datetime.fromisoformat(row["window_start"])
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if (now - start).total_seconds() > window_seconds:
                conn.execute("UPDATE counters SET window_start = ?, count = ? "
                             "WHERE name = ?", (now.isoformat(), n, name))
                return n <= limit
            if row["count"] + n > limit:
                return False
            conn.execute("UPDATE counters SET count = count + ? WHERE name = ?",
                         (n, name))
            return True
