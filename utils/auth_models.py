"""
utils/auth_models.py — SQLite user database for RSSS authentication.

Passwords are stored as salted PBKDF2-SHA256 hashes (werkzeug format).
Accounts created before that still hold a plain SHA-256 digest; it is
accepted once and replaced with the new hash at that user's next login.
Database file: config/users.db (not in git — it holds every account).
"""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

_DB_PATH = Path(__file__).parent.parent / "config" / "users.db"
_METHOD  = "pbkdf2:sha256:600000"


def hash_password(password: str) -> str:
    return generate_password_hash(password, method=_METHOD)


def _is_legacy(stored: str) -> bool:
    """Old unsalted SHA-256 hex digest (64 hex chars, no '$' separators)."""
    return len(stored) == 64 and all(c in "0123456789abcdef" for c in stored.lower())


def check_password(stored: str | None, password: str) -> tuple[bool, bool]:
    """(matches, needs_rehash) for a stored hash."""
    if not stored:
        return False, False
    if _is_legacy(stored):
        ok = hmac.compare_digest(stored, hashlib.sha256(password.encode()).hexdigest())
        return ok, ok
    try:
        return check_password_hash(stored, password), False
    except ValueError:          # unknown/corrupt hash format
        return False, False


class UserDatabase:
    def __init__(self, db_path: str | Path = _DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    username      TEXT PRIMARY KEY,
                    password      TEXT NOT NULL,
                    full_name     TEXT,
                    role          TEXT NOT NULL DEFAULT 'health_center',
                    district      TEXT,
                    hospital      TEXT,
                    health_center TEXT,
                    email         TEXT,
                    active        INTEGER NOT NULL DEFAULT 1
                )
            """)
            # Self-service account requests (added Oct 2026): a request is an
            # inactive account with status 'pending' until an admin approves it.
            have = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
            for col, ddl in (("status", "TEXT NOT NULL DEFAULT 'active'"),
                             ("phone", "TEXT"), ("position", "TEXT"),
                             ("requested_at", "TEXT"), ("reviewed_by", "TEXT"),
                             ("reviewed_at", "TEXT")):
                if col not in have:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
            conn.commit()

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def create_user(
        self,
        username: str,
        password: str,
        full_name: str = "",
        role: str = "health_center",
        district: str | None = None,
        hospital: str | None = None,
        health_center: str | None = None,
        email: str = "",
    ) -> bool:
        hashed = hash_password(password)
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO users "
                    "(username, password, full_name, role, district, hospital, health_center, email) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (username, hashed, full_name, role, district, hospital, health_center, email),
                )
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_user(self, username: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username=?", (username,)
            ).fetchone()
        return dict(row) if row else None

    def verify_password(self, username: str, password: str) -> bool:
        user = self.get_user(username)
        if not user or not user.get("active"):
            return False
        ok, rehash = check_password(user["password"], password)
        if rehash:
            self.update_user(username, password=password)   # upgrade the legacy hash
        return ok

    def login_status(self, username: str, password: str) -> str:
        """'ok', 'pending' (request not yet approved), 'rejected', 'inactive'
        or 'bad' (unknown user or wrong password) — so the sign-in page can
        tell a waiting applicant what is happening."""
        user = self.get_user(username)
        if not user:
            return "bad"
        ok, rehash = check_password(user["password"], password)
        if not ok:
            return "bad"
        status = user.get("status") or "active"
        if status in ("pending", "rejected"):
            return status
        if not user.get("active"):
            return "inactive"
        if rehash:
            self.update_user(username, password=password)
        return "ok"

    def get_all_users(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT username, full_name, role, district, hospital, "
                "health_center, email, active, status FROM users "
                "WHERE COALESCE(status, 'active') <> 'rejected' ORDER BY username"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Account requests ──────────────────────────────────────────────────────

    def request_account(self, username: str, password: str, full_name: str, email: str,
                        phone: str, position: str, role: str, district: str | None,
                        hospital: str | None, health_center: str | None) -> bool:
        """Store a sign-up request as an inactive, pending account."""
        from datetime import datetime
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO users (username, password, full_name, role, district, "
                    "hospital, health_center, email, active, status, phone, position, "
                    "requested_at) VALUES (?,?,?,?,?,?,?,?,0,'pending',?,?,?)",
                    (username, hash_password(password), full_name, role, district, hospital,
                     health_center, email, phone, position,
                     datetime.now().isoformat(timespec="seconds")))
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def pending_requests(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT username, full_name, role, district, hospital, health_center, "
                "email, phone, position, requested_at FROM users "
                "WHERE status = 'pending' ORDER BY requested_at").fetchall()
        return [dict(r) for r in rows]

    def review_request(self, username: str, approve: bool, reviewer: str) -> bool:
        """Approve (activate) or reject a pending request. Rejected requests
        stay in the table (hidden from the account list) as a record."""
        from datetime import datetime
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE users SET status=?, active=?, reviewed_by=?, reviewed_at=? "
                "WHERE username=? AND status='pending'",
                ("active" if approve else "rejected", int(approve), reviewer,
                 datetime.now().isoformat(timespec="seconds"), username))
            conn.commit()
            return cur.rowcount == 1

    def update_user(self, username: str, **fields) -> bool:
        if "password" in fields:
            fields["password"] = hash_password(fields["password"])
        if not fields:
            return False
        sets   = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [username]
        with self._conn() as conn:
            conn.execute(f"UPDATE users SET {sets} WHERE username=?", values)
            conn.commit()
        return True

    def delete_user(self, username: str) -> bool:
        with self._conn() as conn:
            conn.execute("DELETE FROM users WHERE username=?", (username,))
            conn.commit()
        return True

    def set_active(self, username: str, active: bool) -> bool:
        return self.update_user(username, active=int(active))
