"""
tools/reset_admin.py — Reset (or create) the admin login for the RSSS app.

Usage (from the project root):
    python tools/reset_admin.py                 # sets admin / ChangeMe@2026
    python tools/reset_admin.py mypassword      # sets admin / mypassword
    python tools/reset_admin.py user pass        # sets user / pass (ministry)

Stop the running app first (Ctrl-C) so the SQLite file isn't locked.
"""
import hashlib
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "config" / "users.db"


def main() -> int:
    if len(sys.argv) >= 3:
        username, password = sys.argv[1], sys.argv[2]
    else:
        username = "admin"
        password = sys.argv[1] if len(sys.argv) == 2 else "ChangeMe@2026"

    h = hashlib.sha256(password.encode()).hexdigest()
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM users WHERE username=?", (username,))
    if cur.fetchone():
        cur.execute("UPDATE users SET password=?, active=1 WHERE username=?",
                    (h, username))
    else:
        cur.execute(
            "INSERT INTO users (username, password, full_name, role, active) "
            "VALUES (?,?,?,?,1)",
            (username, h, username.title(), "ministry"),
        )
    con.commit()
    con.close()
    print(f"✅ Login ready →  username: {username}   password: {password}")
    print("   Log in, then change it if you like.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
