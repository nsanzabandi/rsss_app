"""
app.py — RSSS Plotly Dash entry point.

Run (dev):    python app.py
Run (prod):   gunicorn app:server -w 1 -k gevent --threads 4 -b 0.0.0.0:8050

Sub-path:     The app is served at  <server_ip>/rsss_app/  via Nginx.
              requests_pathname_prefix and routes_pathname_prefix mount Dash
              at that prefix so all internal _dash-* endpoints resolve correctly.
"""
from __future__ import annotations

import os
import sys
import threading
import warnings
from pathlib import Path

# Suppress pandas FutureWarnings and SQLAlchemy warnings globally
warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', '.*pandas only supports SQLAlchemy.*')
import pandas as pd
#pd.set_option('future.no_silent_downcasting', True)

BASE_DIR = Path(__file__).parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, html, dcc

# ── Plotly thread safety ───────────────────────────────────────────────────────
# Plotly Express reads a shared default template that is built lazily on first
# use. Under gunicorn's gthread worker, two callbacks drawing their first charts
# at the same moment (typically just after a restart) race on that half-built
# object → "ValueError: Invalid value". Build it once now, and let px calls take
# turns (each takes milliseconds).
def _make_plotly_thread_safe() -> None:
    import functools
    import threading
    import plotly.express as px

    lock = threading.RLock()
    for name in dir(px):
        fn = getattr(px, name)
        if callable(fn) and getattr(fn, "__module__", "") == "plotly.express._chart_types":
            @functools.wraps(fn)
            def _locked(*args, _fn=fn, **kwargs):
                with lock:
                    return _fn(*args, **kwargs)
            setattr(px, name, _locked)
    px.scatter(pd.DataFrame({"x": [0], "y": [0]}), x="x", y="y")   # build the template now


_make_plotly_thread_safe()


# ── App ────────────────────────────────────────────────────────────────────────

# URL prefix — must match the Nginx location block and all href links.
# Change this if the app is ever moved to a different sub-path.
URL_PREFIX = "/rsss_app"

app = dash.Dash(
    __name__,
    external_stylesheets=[
        dbc.themes.BOOTSTRAP,
        dbc.icons.BOOTSTRAP,
        "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap",
    ],
    suppress_callback_exceptions=True,
    title="RSSS · NHIC — Rwanda Stunting Surveillance",
    update_title=None,
    meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1"}],
    # Sub-path mounting — Nginx proxies /rsss_app/ to this gunicorn instance.
    requests_pathname_prefix=f"{URL_PREFIX}/",
    routes_pathname_prefix=f"{URL_PREFIX}/",
)

server = app.server
server.secret_key                    = os.environ.get("SECRET_KEY", "rsss-change-me!")
server.config["PERMANENT_SESSION_LIFETIME"] = 86400 * 7   # 7 days

# ── Layout ─────────────────────────────────────────────────────────────────────

from components.layout import serve_layout
app.layout = serve_layout   # function → evaluated per request, reads flask.session


# ── Flask auth routes ──────────────────────────────────────────────────────────

from flask import redirect, request, session

@server.route("/rsss_app/do-login", methods=["POST"])
def do_login():
    from auth import authenticate_user, login as auth_login
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    user = authenticate_user(username, password)
    if user:
        auth_login(user)
        return redirect("/rsss_app/")
    from utils.auth_models import UserDatabase
    status = UserDatabase().login_status(username.lower(), password)
    if status in ("pending", "rejected"):
        return redirect(f"/rsss_app/login?{status}=1")
    return redirect("/rsss_app/login?login_error=1")


# ── Account requests (self-service sign-up, approved by a ministry admin) ──────

_REG_LIMIT_PER_IP, _REG_LIMIT_TOTAL, _REG_WINDOW = 5, 60, 3600
_reg_log: list[tuple[float, str]] = []


def _register_allowed(ip: str) -> bool:
    """Soft anti-spam limit: per client and overall, per hour."""
    import time
    now = time.time()
    _reg_log[:] = [(t, i) for t, i in _reg_log if now - t < _REG_WINDOW]
    if len(_reg_log) >= _REG_LIMIT_TOTAL or sum(i == ip for _, i in _reg_log) >= _REG_LIMIT_PER_IP:
        return False
    _reg_log.append((now, ip))
    return True


@server.route("/rsss_app/do-register", methods=["POST"])
def do_register():
    import re
    f = {k: (request.form.get(k) or "").strip() for k in
         ("full_name", "position", "email", "phone", "role", "district", "hospital",
          "health_center", "username", "website")}
    password = request.form.get("password") or ""
    back = "/rsss_app/register?error="
    if f["website"]:                       # honeypot filled → a bot; pretend it worked
        return redirect("/rsss_app/register?sent=1")
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "?")
    if not _register_allowed(ip):
        return redirect(back + "busy")
    username = f["username"].lower()
    role = f["role"] if f["role"] in ("district", "hospital", "health_center") else ""
    if not all((f["full_name"], f["position"], f["email"], f["phone"], role, username)):
        return redirect(back + "fields")
    if not re.match(r"^[a-z0-9._-]{3,32}$", username):
        return redirect(back + "username")
    if len(password) < 8:
        return redirect(back + "password")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", f["email"]):
        return redirect(back + "email")
    # Same consistency rules as the admin form (pages/settings.py).
    from pages.settings import _geo_options
    geo = _geo_options()
    district, hospital, facility = f["district"] or None, f["hospital"] or None, f["health_center"] or None
    if role == "district":
        hospital = facility = None
    if role == "hospital":
        facility = None
    need = {"district": district, "hospital": hospital, "health_center": facility}[role]
    if not need or (district and geo["district"] and district not in geo["district"]):
        return redirect(back + "area")
    if facility and geo["fac_hospital"].get(facility, hospital) != hospital:
        return redirect(back + "area")
    if hospital and geo["hosp_district"].get(hospital, district) != district:
        return redirect(back + "area")
    from utils.auth_models import UserDatabase
    if not UserDatabase().request_account(
            username, password, f["full_name"][:120], f["email"][:120], f["phone"][:40],
            f["position"][:120], role, district, hospital, facility):
        return redirect(back + "taken")
    _notify_approvers(f["full_name"], role, facility or hospital or district)
    return redirect("/rsss_app/register?sent=1")


def _notify_approvers(name: str, role: str, area: str) -> None:
    """Email ACCOUNT_APPROVER_EMAILS about a new request, in the background so
    a slow mail server never holds up the applicant's page."""
    from config.app_config import ACCOUNT_APPROVER_EMAILS, APP_BASE_URL
    if not ACCOUNT_APPROVER_EMAILS:
        return

    def _send():
        try:
            from core.email_sender import EmailSender
            sender = EmailSender()
            if not sender.connect():
                return
            body = (f"{name} has requested an RSSS account ({role.replace('_', ' ')}, {area}).\n\n"
                    f"Review it in Settings → Users & roles:\n{APP_BASE_URL}/settings\n")
            for to in ACCOUNT_APPROVER_EMAILS:
                sender.send_email(to_email=to, cc_emails=[], subject="RSSS account request",
                                  body=body, attachments=[])
            sender.disconnect()
        except Exception as exc:
            print(f"[register] approver notification failed: {exc}")
    threading.Thread(target=_send, daemon=True).start()

@server.route("/rsss_app/logout")
def logout_route():
    from auth import logout
    logout()
    return redirect("/rsss_app/")


@server.route("/view")
def view_route_unprefixed():
    """Links built before APP_BASE_URL gained its /rsss_app prefix."""
    return redirect(f"/rsss_app/view?{request.query_string.decode()}")


@server.route("/rsss_app/view")
def view_route():
    """View-only scoped dashboard via a signed token (used in report emails)."""
    from auth import user_from_view_token, login as auth_login, get_current_user
    current = get_current_user()
    if current and not current.get("readonly"):
        return redirect("/rsss_app/")          # already signed in — don't downgrade to view-only
    token = request.args.get("token", "")
    user = user_from_view_token(token)
    if not user:
        return redirect("/rsss_app/login?link_expired=1")
    auth_login(user)
    return redirect("/rsss_app/")


@server.route("/rsss_app/test-email")
def test_email_route():
    """Send a quick test email to TEST_EMAIL — open this URL while logged in."""
    from flask import session as s, jsonify
    if not s.get("user"):
        return jsonify({"error": "not authenticated"}), 401
    if s["user"].get("role") != "ministry" or s["user"].get("readonly"):
        return jsonify({"error": "ministry administrators only"}), 403
    try:
        from core.email_sender import EmailSender
        from config.app_config import TEST_EMAIL
        sender = EmailSender()
        sender.connect()
        sender.send_email(
            to_email   = TEST_EMAIL,
            cc_emails  = [],
            subject    = "RSSS — Test Email",
            body       = (
                "This is a test email from the RSSS system.\n\n"
                "If you received this, email delivery is working correctly.\n\n"
                "RSSS Rwanda Stunting Surveillance System"
            ),
            attachments= [],
        )
        sender.disconnect()
        return jsonify({"ok": True, "sent_to": TEST_EMAIL})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Callback gatekeeper (fail closed) ──────────────────────────────────────────
# Dash callbacks are plain POSTs to /_dash-update-component — reachable by
# anyone, whatever the page shows. A visitor who is NOT signed in may only
# trigger the public pages' callbacks (routing, main dashboard, at-risk
# dashboard), and never the download ones. Everything else — follow-up
# search/save, eBuzima, reports, settings, sync, any page added later — gets 403.
_PUBLIC_OUTPUTS = ("page-content.", "db-", "rd-", "risk-tab-content.")
_PUBLIC_DENY    = ("db-download.", "db-dl-msg.", "rd-download.", "rd-dl-msg.")


@server.before_request
def _guard_callbacks():
    if request.path != f"{URL_PREFIX}/_dash-update-component":
        return None
    user = session.get("user")
    if user and not user.get("view_link"):
        return None
    # Not signed in, or signed in through an email view link: the same
    # dashboards-only allow-list (a view link is scoped to its catchment by
    # role, but must not reach child lists or follow-up search either).
    from config.app_config import PUBLIC_VIEW
    out = str((request.get_json(silent=True) or {}).get("output", ""))
    ids = [i for i in out.replace("...", "\n").replace("..", "").split("\n") if i]
    allowed = (PUBLIC_VIEW or user) and ids and all(
        i.startswith(_PUBLIC_OUTPUTS) and not i.startswith(_PUBLIC_DENY) for i in ids)
    if not allowed:
        return ("Sign in required", 403)
    return None


# ── Routing callback ───────────────────────────────────────────────────────────

_PUBLIC_PATHS = ("/", "/dashboard", "/risk")


@app.callback(Output("page-content", "children"),
              Input("url", "pathname"), State("url", "search"))
def route(pathname: str, search: str):
    from auth import has_min_role, current_user, is_aggregate_only
    user = current_user()
    if not user:
        return html.Div()

    # Strip the URL prefix so routing logic is identical regardless of sub-path.
    p = (pathname or URL_PREFIX + "/").rstrip("/")
    p = p[len(URL_PREFIX):]   # e.g. "/rsss_app/reports" → "/reports"
    path = p or "/"
    readonly = bool(user.get("readonly"))

    if path == "/login":
        from urllib.parse import parse_qs
        from components.layout import login_layout, login_message
        args = {k: v[0] for k, v in parse_qs((search or "").lstrip("?")).items()}
        return login_layout(login_message(args), embedded=True)
    if path == "/register":
        from urllib.parse import parse_qs
        from components.layout import register_layout, register_message
        if not is_aggregate_only(user):
            return _forbidden("You're already signed in.")
        args = {k: v[0] for k, v in parse_qs((search or "").lstrip("?")).items()}
        msg, ok = register_message(args)
        return register_layout(msg, ok, embedded=True)
    if is_aggregate_only(user) and path not in _PUBLIC_PATHS:
        return _signin_prompt()

    if path in ("/", "/dashboard"):
        from pages.dashboard import layout
        return layout(user)

    # View-only (email link) users can see the dashboard and follow-up analytics,
    # but not the operational pages (reports/send, eTracker sync).
    if readonly and path in ("/reports",):
        return _forbidden("This is a view-only dashboard for your catchment area.")

    if path == "/reports":
        if not has_min_role(user, "hospital"):
            return _forbidden("Reports are available to hospital level and above.")
        from pages.reports import layout
        return layout(user)

    if path == "/followup":
        from pages.followup import layout
        return layout(user)

    if path == "/risk":
        from pages.risk import layout
        return layout(user)

    if path == "/ebuzima":
        from pages.ebuzima import layout
        return layout(user)

    if path == "/profile":
        if readonly:
            return _forbidden("View-only links don't have a profile.")
        from pages.profile import layout
        return layout(user)

    if path == "/settings":
        if user.get("role") != "ministry" or readonly:
            return _forbidden("Settings are available to ministry administrators only.")
        from pages.settings import layout
        return layout(user)

    return _not_found(path)


def _signin_prompt() -> html.Div:
    return dbc.Container(dbc.Card(dbc.CardBody([
        html.Div([html.I(className="bi bi-lock"), "Sign in to see this page"],
                 className="nhic-card-title"),
        html.P("This part of RSSS is for health staff. The public view shows the national "
               "dashboard and at-risk dashboard.", className="nhic-hint mt-2"),
        dcc.Link("Sign in", href=f"{URL_PREFIX}/login", className="btn btn-nhic btn-sm"),
    ]), className="nhic-card mt-4", style={"maxWidth": "520px"}), className="py-4")


def _forbidden(msg: str = "") -> html.Div:
    return dbc.Container(
        dbc.Alert([html.I(className="bi bi-shield-x me-2"),
                   msg or "Access denied."], color="danger", className="mt-4"),
        className="py-4")


def _not_found(path: str) -> html.Div:
    return dbc.Container(
        dbc.Alert([html.I(className="bi bi-exclamation-triangle me-2"),
                   f"Page not found: {path}"], color="warning", className="mt-4"),
        className="py-4")


# ── Register page callbacks ────────────────────────────────────────────────────

from pages.dashboard import register_callbacks as _reg_dashboard
from pages.reports   import register_callbacks as _reg_reports
from pages.followup  import register_callbacks as _reg_followup
from pages.risk      import register_callbacks as _reg_risk
from pages.ebuzima   import register_callbacks as _reg_ebuzima
from pages.settings  import register_callbacks as _reg_settings
from pages.profile   import register_callbacks as _reg_profile
from pages.risk_dashboard import register_callbacks as _reg_risk_dash

_reg_dashboard(app)
_reg_reports(app)
_reg_followup(app)
_reg_risk(app)
_reg_ebuzima(app)
_reg_settings(app)
_reg_profile(app)
_reg_risk_dash(app)


# ── Automatic background sync ──────────────────────────────────────────────────
# Set AUTO_SYNC_HOURS in .env (e.g. 6) to pull new eTracker records on a schedule
# and refresh the dashboard caches automatically. 0/unset = disabled.

def _start_auto_sync() -> None:
    import threading
    import time as _t

    try:
        hours = float(os.environ.get("AUTO_SYNC_HOURS", "0") or 0)
    except ValueError:
        hours = 0
    if hours <= 0:
        return

    def _loop():
        while True:
            _t.sleep(hours * 3600)
            try:
                print("[auto-sync] running incremental sync…")
                from sync.etl import run_incremental_sync
                run_incremental_sync()
                import data
                data.refresh_after_sync()    # incremental, in the background
                print("[auto-sync] done — dashboard refresh started.")
            except Exception as exc:
                print(f"[auto-sync] failed: {exc}")

    threading.Thread(target=_loop, daemon=True).start()
    print(f"[auto-sync] enabled — every {hours} h")


def _start_auto_report() -> None:
    """Send monthly reports automatically on the scheduled day-of-month
    (configured on the Reports page). Sends the PREVIOUS month's reports."""
    import threading
    import time as _t
    from datetime import datetime, timedelta

    def _loop():
        last_run_month = None
        while True:
            try:
                import jobs
                sched = jobs.load_report_schedule()
                now = datetime.now()
                month_key = now.strftime("%Y-%m")
                if (sched.get("enabled") and now.day == int(sched.get("day", 1))
                        and last_run_month != month_key):
                    prev = now.replace(day=1) - timedelta(days=1)
                    level = sched.get("level", "hospital")
                    targets = jobs.list_targets(level)
                    if targets:
                        print(f"[auto-report] sending {len(targets)} {level} reports "
                              f"for {prev.month:02d}/{prev.year}")
                        starter = (jobs.start_hospital_job if level == "hospital"
                                   else jobs.start_facility_job)
                        starter(targets, prev.month, prev.year, True,
                                "auto-scheduler", dry_run=bool(sched.get("dry_run")))
                    last_run_month = month_key
            except Exception as exc:
                print(f"[auto-report] check failed: {exc}")
            _t.sleep(3600)   # check hourly

    threading.Thread(target=_loop, daemon=True).start()
    print("[auto-report] scheduler active")


# Only start the scheduler in the actual serving process (avoid the Flask
# debug reloader's parent process starting a duplicate).
def _preload_data() -> None:
    """Load the dashboard data at startup (from the disk cache, ~10s) so the
    first visitor after a restart doesn't wait for it."""
    import threading

    def _work():
        try:
            import data
            data.warm_child_level()
        except Exception as exc:
            print(f"[startup] data preload failed: {exc}")
    threading.Thread(target=_work, daemon=True).start()


if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not os.environ.get("DEBUG", "true").lower() == "true":
    _start_auto_sync()
    _start_auto_report()
    _preload_data()


# ── Dev server ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 8050))
    debug = os.environ.get("DEBUG", "true").lower() == "true"
    # threaded=True so a slow callback (e.g. DB connection timeout) doesn't
    # block the entire server and cause "server did not respond" errors.
    app.run(debug=debug, host="0.0.0.0", port=port, threaded=True)
