"""
components/layout.py — NHIC app shell (sidebar) + login form.

serve_layout() is assigned to app.layout (as a function, not a value)
so it is called fresh on every page load, reading flask.session each time.
Styling lives in assets/nhic.css (auto-loaded by Dash).
"""
from datetime import datetime
from pathlib import Path

from dash import html, dcc, get_asset_url
import dash_bootstrap_components as dbc

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_BASE       = "/rsss_app"

ORG_SHORT   = "NHIC"
ORG_FULL    = "National Health Intelligence Center"
PRODUCT     = "RSSS"
PRODUCT_FULL = "Rwanda Stunting Surveillance System"
APP_TITLE   = "Stunting Surveillance Dashboard For Immunized Children"


def _logo(full: bool = False) -> html.Img | html.Div:
    """Ministry of Health logo from assets/.
    full=False → emblem only (moh_emblem.png), for small spaces next to our
    own "Republic of Rwanda · Ministry of Health" text; full=True → the full
    logo with its built-in wording (moh_logo.png), for large display."""
    names = ("moh_logo.svg", "moh_logo.png") if full else ("moh_emblem.png", "moh_logo.png")
    for name in names:
        if (_ASSETS_DIR / name).exists():
            return html.Img(src=get_asset_url(name), alt="Ministry of Health, Republic of Rwanda")
    return html.Div(["MoH"], className="nhic-badge", title="Ministry of Health, Rwanda")


def _brand(with_logo: bool = True) -> html.Div:
    """NHIC name block. The sidebar uses the NHIC mark (the MoH logo sits in
    the top bar); the login page shows the MoH logo here."""
    return html.Div([
        _logo() if with_logo else html.Div("NHIC", className="nhic-badge nhic-badge-dark"),
        html.Div([
            html.Div(ORG_SHORT, className="nhic-org"),
            html.Div(ORG_FULL, className="nhic-org-full"),
        ]),
    ], className="nhic-brand")


def _nav_link(label: str, path: str, icon: str) -> dbc.NavLink:
    # title= gives the full name as a tooltip while the sidebar is collapsed
    return dbc.NavLink([html.I(className=f"bi {icon}", title=label),
                        html.Span(label, className="nhic-label")],
                       href=f"{_BASE}{path}", active="exact", className="nhic-link")


def _section(title: str, links: list) -> list:
    return [html.Div(title, className="nhic-section"), *links] if links else []


def _freshness() -> html.Div:
    """'Data synced …' pill: when eTracker data last reached the dashboard
    (the computed cache's data_as_of watermark). Cheap — one small JSON read."""
    try:
        from data import _read_meta
        stamp = _read_meta().get("data_as_of")
        synced = datetime.fromisoformat(stamp) if stamp else None
    except Exception:
        synced = None
    if synced is None:
        return html.Div([html.Span(className="nhic-dot bad"),
                         html.Div(["No synced data yet", html.Small("Run an eTracker sync")])],
                        className="nhic-fresh")
    age_days = (datetime.now() - synced).total_seconds() / 86400
    level = "ok" if age_days <= 2 else "warn" if age_days <= 7 else "bad"
    return html.Div([
        html.Span(className=f"nhic-dot {level}"),
        html.Div([f"Data synced {synced:%d %b %Y}",
                  html.Small(f"{synced:%H:%M} · eTracker")]),
    ], className="nhic-fresh", title="Last time new eTracker data reached the dashboard")


def _initials(name: str) -> str:
    parts = [p for p in (name or "").replace(".", " ").split() if p]
    return ("".join(p[0] for p in parts[:2]) or "?").upper()


def _sidebar(user: dict | None = None) -> html.Div:
    user     = user or {}
    role     = user.get("role", "")
    readonly = bool(user.get("readonly"))
    is_admin = role == "ministry" and not readonly
    public   = bool(user.get("public"))

    analytics  = [_nav_link("Dashboard",        "/",         "bi-speedometer2"),
                  _nav_link("At-Risk Children", "/risk",     "bi-exclamation-triangle")]
    # Same rule as the /reports route: hospital level and above, not view-only links.
    from auth import has_min_role
    can_report = has_min_role(user, "hospital") and not readonly
    operations = [_nav_link("Monthly Reports", "/reports", "bi-file-earmark-text")] if can_report else []
    operations += [_nav_link("Follow-Up", "/followup", "bi-clipboard2-pulse")]
    nutrition  = [_nav_link("eBuzima",          "/ebuzima",  "bi-clipboard2-data")]
    if public:                      # public view: dashboards only
        operations, nutrition = [], []
    admin      = [_nav_link("Settings",         "/settings", "bi-gear")] if is_admin else []

    return html.Div([
        html.Div(className="nhic-flag"),
        _brand(with_logo=False),
        html.Div([html.Div(PRODUCT, className="nhic-product-name"),
                  html.Div("Stunting Surveillance", className="nhic-product-desc")],
                 className="nhic-product"),
        dbc.Nav(
            _section("Analytics", analytics) + _section("Operations", operations)
            + _section("Nutrition", nutrition) + _section("Admin", admin),
            vertical=True, className="nhic-nav"),
        html.Div([
            _freshness(),
            html.Div("Public view — sign in for reports, follow-up and child lists.",
                     className="nhic-partner") if public else None,
            html.Div("Ministry of Health · in partnership with RBC", className="nhic-partner"),
        ], className="nhic-foot"),
    ], id="sidebar", className="nhic-sidebar")


def _topbar(user: dict) -> html.Header:
    """Ministry of Health identity on the left; the signed-in user's menu
    (profile, sign out) in the top-right corner."""
    from auth import role_label
    name     = user.get("full_name") or user.get("username", "")
    readonly = bool(user.get("readonly"))
    if user.get("public"):
        right = dcc.Link([html.I(className="bi bi-box-arrow-in-right me-1"), "Sign in"],
                         href=f"{_BASE}/login", className="btn btn-nhic btn-sm nhic-signin")
        return html.Header([
            html.Div([_logo(), html.Span("Republic of Rwanda", className="nhic-top-country"),
                      html.Span(className="nhic-top-sep"),
                      html.Span("Ministry of Health", className="nhic-top-ministry")],
                     className="nhic-top-id"),
            html.Div(APP_TITLE, className="nhic-top-title"),
            html.Div(right, className="nhic-usermenu"),
        ], className="nhic-topbar")
    items = [dbc.DropdownMenuItem([html.Div(name, className="fw-semibold"),
                                   html.Div(role_label(user.get("role", ""))
                                            + (" · view only" if readonly else ""),
                                            className="nhic-hint")], header=True)]
    if not readonly:
        items.append(dbc.DropdownMenuItem([html.I(className="bi bi-person me-2"), "My profile"],
                                          href=f"{_BASE}/profile"))
    items += [dbc.DropdownMenuItem(divider=True),
              dbc.DropdownMenuItem([html.I(className="bi bi-box-arrow-right me-2"), "Sign out"],
                                   href=f"{_BASE}/logout", external_link=True,
                                   className="text-danger")]
    return html.Header([
        html.Div([
            _logo(),
            html.Span("Republic of Rwanda", className="nhic-top-country"),
            html.Span(className="nhic-top-sep"),
            html.Span("Ministry of Health", className="nhic-top-ministry"),
        ], className="nhic-top-id"),
        html.Div(APP_TITLE, className="nhic-top-title"),
        dbc.DropdownMenu(
            items, align_end=True, color="link", className="nhic-usermenu",
            label=html.Span([html.Span(_initials(name), className="nhic-avatar"),
                             html.Span(name, className="nhic-top-name")],
                            className="d-flex align-items-center gap-2")),
    ], className="nhic-topbar")


def _app_shell(user: dict) -> html.Div:
    return html.Div([
        _sidebar(user),
        html.Main([
            _topbar(user),
            html.Div(id="page-content", className="nhic-content", style={"maxWidth": "1400px"}),
        ], className="nhic-main"),
    ], className="nhic-shell d-flex")


def login_layout(error: str = "", embedded: bool = False) -> html.Div:
    """Login form — uses a plain HTML POST so Flask handles auth and redirects.
    embedded=True → shown as a page inside the public shell."""
    return html.Div(html.Div([
        html.Div([
            html.Div(className="nhic-flag"),
            _brand(with_logo=False),
            html.Div(PRODUCT, className="nhic-login-title"),
            html.Div(PRODUCT_FULL, className="nhic-login-sub fw-semibold"),
            html.Div("Monthly stunting surveillance, at-risk follow-up and reporting "
                     "for every district hospital and health facility.",
                     className="nhic-login-sub mt-2"),
            html.Div("Ministry of Health, Rwanda · in partnership with RBC",
                     className="nhic-login-foot"),
        ], className="nhic-login-brand"),

        html.Div([
            html.Div(_logo(full=True), className="nhic-login-logo"),
            html.H5("Sign in"),
            html.P("Use the account issued by NHIC.", className="nhic-hint mb-4"),
            dbc.Alert(error, color="danger", className="py-2", is_open=bool(error)),
            # Plain HTML form — POSTs to Flask route, triggers real page reload
            html.Form([
                html.Label("Username", htmlFor="login-username", className="mb-1"),
                dcc.Input(id="login-username", name="username", type="text",
                          autoComplete="username", autoFocus=True, required=True,
                          className="form-control mb-3"),
                html.Label("Password", htmlFor="login-password", className="mb-1"),
                dcc.Input(id="login-password", name="password", type="password",
                          autoComplete="current-password", required=True,
                          className="form-control mb-4"),
                html.Button("Sign in", type="submit", className="btn btn-nhic w-100 py-2"),
            ], action=f"{_BASE}/do-login", method="post"),
        ], className="nhic-login-form"),
    ], className="nhic-login"), className="nhic-login-bg" + (" nhic-login-embedded" if embedded else ""))


def serve_layout() -> html.Div:
    """Called by Dash on every page request — reads Flask session."""
    from flask import session as flask_session, request as flask_req
    from auth import current_user
    user = current_user()            # signed-in user, else the public visitor
    if not user:                     # public view switched off → login page
        try:
            err = "Invalid username or password." if flask_req.args.get("login_error") else ""
        except Exception:
            err = ""
        return html.Div([
            dcc.Location(id="url", refresh=False),
            html.Div(login_layout(err), id="root-container"),
        ])
    return html.Div([
        dcc.Location(id="url", refresh=False),
        html.Div(_app_shell(user), id="root-container"),
    ])
