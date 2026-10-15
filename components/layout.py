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
    if public or user.get("view_link"):     # public view / email link: dashboards only
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
            html.Div("View-only link — sign in for follow-up and child lists.",
                     className="nhic-partner") if user.get("view_link") else None,
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
            html.Div(["No account yet? ",
                      html.A("Request access", href=f"{_BASE}/register")],
                     className="nhic-hint mt-3 text-center"),
        ], className="nhic-login-form"),
    ], className="nhic-login"), className="nhic-login-bg" + (" nhic-login-embedded" if embedded else ""))


_REQUEST_ROLES = [("health_center", "Health centre staff"), ("hospital", "Hospital staff"),
                  ("district", "District officer")]


def register_layout(message: str = "", ok: bool = False, embedded: bool = False) -> html.Div:
    """Account request form — a plain HTML POST to /do-register (works with
    the public view on or off). The request waits for a ministry admin's
    approval in Settings → Users & roles."""
    from config.app_config import ACCOUNT_HELP_NAME, ACCOUNT_HELP_PHONE
    try:
        from pages.settings import _geo_options
        geo = _geo_options()
    except Exception:
        geo = {"district": [], "district_hospital": [], "health_facility": []}

    def _select(name, label, values, placeholder, required=False):
        return html.Div([
            html.Label(label, htmlFor=f"reg-{name}", className="mb-1"),
            html.Select([html.Option(placeholder, value="")]
                        + [html.Option(v, value=v) for v in values],
                        id=f"reg-{name}", name=name, required=required,
                        className="form-select mb-3"),
        ])

    def _input(name, label, kind="text", auto=None, hint=None, required=True):
        return html.Div([
            html.Label(label, htmlFor=f"reg-{name}", className="mb-1"),
            dcc.Input(id=f"reg-{name}", name=name, type=kind, required=required,
                      autoComplete=auto, className="form-control" + ("" if hint else " mb-3")),
            html.Div(hint, className="nhic-hint mb-3") if hint else None,
        ])

    help_box = html.Div([
        html.Div("Need help with your request?", className="fw-semibold"),
        html.Div(f"{ACCOUNT_HELP_NAME} · {ACCOUNT_HELP_PHONE}"),
    ], className="nhic-hint mt-3")

    if ok:
        body = [html.H5("Request sent"),
                dbc.Alert(message, color="success", className="py-2"),
                html.P("You'll be able to sign in once an administrator approves your "
                       "account. This usually takes one working day.", className="nhic-hint"),
                html.A("Back to sign in", href=f"{_BASE}/login", className="btn btn-nhic btn-sm"),
                help_box]
    else:
        body = [
            html.H5("Request access"),
            html.P("For health staff who receive RSSS reports. An administrator checks "
                   "every request before the account can sign in.", className="nhic-hint mb-3"),
            dbc.Alert(message, color="danger", className="py-2", is_open=bool(message)),
            html.Form([
                _input("full_name", "Full name", auto="name"),
                _input("position", "Position / job title", hint="e.g. Nutrition officer, Gisenyi DH"),
                _input("email", "Work email", kind="email", auto="email"),
                _input("phone", "Phone number", kind="tel", auto="tel"),
                html.Div([
                    html.Label("I work at", htmlFor="reg-role", className="mb-1"),
                    html.Select([html.Option(l, value=v) for v, l in _REQUEST_ROLES],
                                id="reg-role", name="role", className="form-select mb-3"),
                ]),
                _select("district", "District", geo["district"], "Select your district", required=True),
                _select("hospital", "Hospital (hospital or health-centre staff)",
                        geo["district_hospital"], "Select your hospital"),
                _select("health_center", "Health centre (health-centre staff)",
                        geo["health_facility"], "Select your health centre"),
                _input("username", "Choose a username", auto="username",
                       hint="3–32 characters: lowercase letters, digits, . _ -"),
                _input("password", "Choose a password", kind="password", auto="new-password",
                       hint="At least 8 characters"),
                # Honeypot: hidden from people, filled in by form-spamming bots.
                html.Div(dcc.Input(name="website", type="text", autoComplete="off"),
                         style={"position": "absolute", "left": "-9999px"}, **{"aria-hidden": "true"}),
                html.Button("Send request", type="submit", className="btn btn-nhic w-100 py-2"),
            ], action=f"{_BASE}/do-register", method="post"),
            html.Div(["Already have an account? ", html.A("Sign in", href=f"{_BASE}/login")],
                     className="nhic-hint mt-3 text-center"),
            help_box,
        ]
    return html.Div(html.Div([
        html.Div([
            html.Div(className="nhic-flag"),
            _brand(with_logo=False),
            html.Div(PRODUCT, className="nhic-login-title"),
            html.Div(PRODUCT_FULL, className="nhic-login-sub fw-semibold"),
            html.Div("Signed-in staff see the children to follow up in their own "
                     "district, hospital or health centre.", className="nhic-login-sub mt-2"),
            html.Div("Ministry of Health, Rwanda · in partnership with RBC",
                     className="nhic-login-foot"),
        ], className="nhic-login-brand"),
        html.Div([html.Div(_logo(full=True), className="nhic-login-logo")] + body,
                 className="nhic-login-form"),
    ], className="nhic-login"), className="nhic-login-bg" + (" nhic-login-embedded" if embedded else ""))


def login_message(args) -> str:
    """Sign-in page message from the redirect's query string."""
    from config.app_config import ACCOUNT_HELP_NAME, ACCOUNT_HELP_PHONE
    if args.get("pending"):
        return ("Your account request is waiting for approval. For help, contact "
                f"{ACCOUNT_HELP_NAME} ({ACCOUNT_HELP_PHONE}).")
    if args.get("rejected"):
        return ("Your account request was not approved. Contact "
                f"{ACCOUNT_HELP_NAME} ({ACCOUNT_HELP_PHONE}) if you think this is a mistake.")
    if args.get("link_expired"):
        return ("This dashboard link has expired or is not valid. Use the link in the "
                "latest monthly report email, or sign in.")
    if args.get("login_error"):
        return "Invalid username or password."
    return ""


_REGISTER_ERRORS = {
    "fields":   "Please fill in every field.",
    "username": "Username: 3–32 characters, lowercase letters, digits, . _ - only.",
    "taken":    "That username is already taken. Choose another.",
    "password": "Password must be at least 8 characters.",
    "email":    "That email address doesn't look valid.",
    "area":     "Choose the district, hospital or health centre you work at. "
                "The hospital must be in the chosen district, and the health centre in that hospital's catchment.",
    "busy":     "Too many requests from this connection. Please try again in an hour.",
}


def register_message(args) -> tuple[str, bool]:
    if args.get("sent"):
        return "Your request has been sent to the RSSS administrators.", True
    return _REGISTER_ERRORS.get(args.get("error", ""), ""), False


def serve_layout() -> html.Div:
    """Called by Dash on every page request — reads Flask session."""
    from flask import session as flask_session, request as flask_req
    from auth import current_user
    user = current_user()            # signed-in user, else the public visitor
    if not user:                     # public view switched off → login page
        try:
            args = flask_req.args
            err = login_message(args)
            if flask_req.path.rstrip("/").endswith("/register"):
                msg, ok = register_message(args)
                return html.Div([dcc.Location(id="url", refresh=False),
                                 html.Div(register_layout(msg, ok), id="root-container")])
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
