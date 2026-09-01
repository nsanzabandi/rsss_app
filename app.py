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
    title="RSSS — Rwanda Stunting Surveillance",
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
    return redirect("/rsss_app/?login_error=1")

@server.route("/rsss_app/logout")
def logout_route():
    from auth import logout
    logout()
    return redirect("/rsss_app/")


@server.route("/rsss_app/view")
def view_route():
    """View-only scoped dashboard via a signed token (used in report emails)."""
    from auth import user_from_view_token, login as auth_login
    token = request.args.get("token", "")
    user = user_from_view_token(token)
    if not user:
        return redirect("/rsss_app/?login_error=1")
    auth_login(user)
    return redirect("/rsss_app/")


@server.route("/rsss_app/test-email")
def test_email_route():
    """Send a quick test email to TEST_EMAIL — open this URL while logged in."""
    from flask import session as s, jsonify
    if not s.get("user"):
        return jsonify({"error": "not authenticated"}), 401
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


# ── Routing callback ───────────────────────────────────────────────────────────

@app.callback(Output("page-content", "children"),
              Input("url", "pathname"))
def route(pathname: str):
    from flask import session as s
    from auth import has_min_role
    user = s.get("user")
    if not user:
        return html.Div()

    # Strip the URL prefix so routing logic is identical regardless of sub-path.
    p = (pathname or URL_PREFIX + "/").rstrip("/")
    p = p[len(URL_PREFIX):]   # e.g. "/rsss_app/reports" → "/reports"
    path = p or "/"
    readonly = bool(user.get("readonly"))

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

    return _not_found(path)


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

_reg_dashboard(app)
_reg_reports(app)
_reg_followup(app)
_reg_risk(app)
_reg_ebuzima(app)


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
                data.invalidate_cache()
                data.warm_child_level()      # rebuild computed tables in background
                print("[auto-sync] done — caches refreshed.")
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
if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not os.environ.get("DEBUG", "true").lower() == "true":
    _start_auto_sync()
    _start_auto_report()


# ── Dev server ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 8050))
    debug = os.environ.get("DEBUG", "true").lower() == "true"
    # threaded=True so a slow callback (e.g. DB connection timeout) doesn't
    # block the entire server and cause "server did not respond" errors.
    app.run(debug=debug, host="0.0.0.0", port=port, threaded=True)
