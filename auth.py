"""
auth.py — Flask session-based authentication helpers for RSSS Dash app.

Dash runs on Flask, so flask.session works natively.
No external auth packages required.
"""
from __future__ import annotations

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
        if db.login_status(user["username"], password) != "ok":
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


# Visitor who hasn't signed in, when the public view is on: national scope,
# read-only. Never stored in the session; pages that list children or allow
# actions must check is_public() / readonly.
PUBLIC_USER = {"username": "public", "full_name": "Public view", "role": "public",
               "readonly": True, "public": True, "email": ""}


def current_user() -> dict | None:
    """The signed-in user, else the public visitor (if the public view is on)."""
    u = session.get("user")
    if u:
        return u
    from config.app_config import PUBLIC_VIEW
    return dict(PUBLIC_USER) if PUBLIC_VIEW else None


def is_public(user: dict | None) -> bool:
    return bool(user and user.get("public"))


def is_aggregate_only(user: dict | None) -> bool:
    """Public visitors and email-link viewers: dashboards and figures only —
    no child lists, names, phone numbers or operational pages."""
    return bool(user and (user.get("public") or user.get("view_link")))


# ── View-only tokenised links (scoped dashboards, no account needed) ───────────
# Sent in the monthly report emails. A link opens the dashboards for that
# catchment only (figures and charts, no child lists — see is_aggregate_only)
# and expires, so a forwarded or old email stops working.

VIEW_LINK_MAX_AGE = 62 * 24 * 3600     # two months: each report's link outlives the next one


def _view_serializer():
    import os
    from itsdangerous import URLSafeTimedSerializer
    secret = os.environ.get("SECRET_KEY", "rsss-change-me!")
    return URLSafeTimedSerializer(secret, salt="rsss-view-link")


def make_view_token(scope_type: str, scope_value: str) -> str:
    """Signed, tamper-proof, expiring token for a view-only dashboard.
    scope_type ∈ {'national', 'district', 'hospital', 'facility'}."""
    return _view_serializer().dumps({"t": scope_type, "v": scope_value})


def read_view_token(token: str) -> dict | None:
    try:
        return _view_serializer().loads(token, max_age=VIEW_LINK_MAX_AGE)
    except Exception:          # bad signature, expired, or malformed
        return None


def user_from_view_token(token: str) -> dict | None:
    """Build a read-only, aggregate-only, catchment-scoped session user."""
    data = read_view_token(token)
    if not data:
        return None
    t, v = data.get("t"), data.get("v")
    if not v:
        return None
    user = {"username": f"view:{t}:{v}",
            "full_name": f"{v} (view only)",
            "readonly": True, "view_link": True, "email": ""}
    if t == "national":
        user.update(role="ministry", full_name="National dashboard (view only)")
    elif t == "hospital":
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
    "public":        "Public view",
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
