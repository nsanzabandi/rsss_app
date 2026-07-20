"""
auth.py — Flask session-based authentication helpers for RSSS Dash app.

Dash runs on Flask, so flask.session works natively.
No external auth packages required.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from flask import session

BASE_DIR = Path(__file__).parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from utils.auth_models import UserDatabase

# ── Role hierarchy (lowest → highest) ─────────────────────────────────────────

_HIERARCHY = ["health_center", "hospital", "district", "ministry"]


def _level(role: str) -> int:
    try:
        return _HIERARCHY.index(role)
    except ValueError:
        return -1


# ── Core auth ──────────────────────────────────────────────────────────────────

def authenticate_user(username: str, password: str) -> dict | None:
    """
    Verify credentials against SQLite user database.
    Returns safe user dict (no password hash) or None.
    """
    try:
        db   = UserDatabase()
        user = db.get_user(username.strip())
        if not user or not user.get("active"):
            return None
        hashed = hashlib.sha256(password.encode()).hexdigest()
        if user["password"] != hashed:
            return None
        return {
            "username":      user["username"],
            "full_name":     user.get("full_name", user["username"]),
            "role":          user["role"],
            "district":      user.get("district"),
            "hospital":      user.get("hospital"),
            "health_center": user.get("health_center"),
            "email":         user.get("email", ""),
        }
    except Exception as exc:
        print(f"[auth] authenticate error: {exc}")
        return None


def get_current_user() -> dict | None:
    return session.get("user")


# ── View-only tokenised links (scoped dashboards, no account needed) ───────────

def _view_serializer():
    import os
    from itsdangerous import URLSafeSerializer
    secret = os.environ.get("SECRET_KEY", "rsss-change-me!")
    return URLSafeSerializer(secret, salt="rsss-view-link")


def make_view_token(scope_type: str, scope_value: str) -> str:
    """Create a signed, tamper-proof token for a view-only scoped dashboard.
    scope_type ∈ {'hospital', 'district', 'facility'}."""
    return _view_serializer().dumps({"t": scope_type, "v": scope_value})


def read_view_token(token: str) -> dict | None:
    try:
        return _view_serializer().loads(token)
    except Exception:
        return None


def user_from_view_token(token: str) -> dict | None:
    """Build a read-only, catchment-scoped session user from a view token."""
    data = read_view_token(token)
    if not data:
        return None
    t, v = data.get("t"), data.get("v")
    if not v:
        return None
    user = {"username": f"view:{t}:{v}",
            "full_name": f"{v} (view only)",
            "readonly": True, "email": ""}
    if t == "hospital":
        user.update(role="hospital", hospital=v)
    elif t == "district":
        user.update(role="district", district=v)
    elif t == "facility":
        user.update(role="health_center", health_center=v)
    else:
        return None
    return user


def login(user: dict) -> None:
    session["user"]      = user
    session.permanent    = True


def logout() -> None:
    session.clear()


# ── Permission helpers ─────────────────────────────────────────────────────────

def has_min_role(user: dict | None, min_role: str) -> bool:
    if not user:
        return False
    return _level(user.get("role", "")) >= _level(min_role)


def is_ministry(user: dict | None) -> bool:
    return has_min_role(user, "ministry")


def is_district_plus(user: dict | None) -> bool:
    return has_min_role(user, "district")


def is_hospital_plus(user: dict | None) -> bool:
    return has_min_role(user, "hospital")


# ── Display helpers ────────────────────────────────────────────────────────────

_ROLE_LABELS = {
    "ministry":      "Ministry",
    "district":      "District Officer",
    "hospital":      "Hospital",
    "health_center": "Health Center",
}


def role_label(role: str) -> str:
    return _ROLE_LABELS.get(role, role.replace("_", " ").title())


def user_display(user: dict) -> str:
    name  = user.get("full_name") or user.get("username", "")
    label = role_label(user.get("role", ""))
    return f"{name} · {label}"
