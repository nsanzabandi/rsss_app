"""
utils/auth_models.py — SQLite user database for RSSS authentication.

Passwords are stored as SHA-256 hex digests.
Database file: config/users.db
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

_DB_PATH = Path(__file__).parent.parent / "config" / "users.db"


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
        hashed = hashlib.sha256(password.encode()).hexdigest()
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
        return user["password"] == hashlib.sha256(password.encode()).hexdigest()

    def get_all_users(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT username, full_name, role, district, hospital, "
                "health_center, email, active FROM users ORDER BY username"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_user(self, username: str, **fields) -> bool:
        if "password" in fields:
            fields["password"] = hashlib.sha256(fields["password"].encode()).hexdigest()
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
