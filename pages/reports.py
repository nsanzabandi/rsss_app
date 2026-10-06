"""
pages/reports.py — Report generation UI.

Hospital or Facility mode, month/year picker,
multi-select targets, email toggle, dry-run checkbox,
live job progress via dcc.Interval polling.
"""
from __future__ import annotations

from datetime import datetime

from dash import Input, Output, State, dcc, html, ctx, no_update
import dash_bootstrap_components as dbc

_MONTH_OPTS = [{"label": f"{m:02d} – {n}", "value": m} for m, n in [
    (1,"January"),(2,"February"),(3,"March"),(4,"April"),
    (5,"May"),(6,"June"),(7,"July"),(8,"August"),
    (9,"September"),(10,"October"),(11,"November"),(12,"December"),
]]
_YEAR_OPTS = [{"label": str(y), "value": y}
              for y in range(datetime.today().year, datetime.today().year - 5, -1)]

_LBL = {"fontSize": "0.78rem", "fontWeight": "600", "color": "#5D6D7E"}
_DD  = {"fontSize": "0.82rem"}


def layout(user: dict) -> html.Div:
    role      = user.get("role", "")
    can_email = role in ("ministry", "district", "hospital")
    now       = datetime.today()
    def_month = now.month - 1 or 12
    def_year  = now.year if now.month > 1 else now.year - 1

    import jobs
    sched = jobs.load_report_schedule()

    return html.Div([
        dbc.Row([
            dbc.Col(html.H5("Generate Reports", className="fw-bold mb-0",
                            style={"color": "#2C3E50"})),
            dbc.Col(html.Span("PDF + Excel reports sent to facility contacts.",
                              className="text-muted", style={"fontSize": "0.78rem"}),
                    className="text-end"),
        ], className="align-items-center mb-3"),

        # ── Automatic monthly sending — summary only; edited in Settings ─────
        # (ministry admins only — it controls emails to every hospital)
        dbc.Alert([
            html.I(className="bi bi-calendar-check me-2"),
            (f"Automatic sending is on: {sched['level']} reports for the previous month "
             f"go out on day {sched['day']} of each month"
             + (" to the test inbox only." if sched["dry_run"] else " to real contacts.")
             if sched["enabled"] else "Automatic monthly sending is off."),
            *([" ", dcc.Link("Change in Settings", href="/rsss_app/settings")]
              if role == "ministry" and not user.get("readonly") else []),
        ], color="light", className="py-2 mb-3 border-0 shadow-sm",
           style={"fontSize": "0.82rem"}),

        dbc.Card(dbc.CardBody([
            dbc.Row([
                dbc.Col([
                    dbc.Label("Report Type", style=_LBL),
                    dbc.RadioItems(id="rpt-type",
                                   options=[{"label": " Hospital Reports",  "value": "hospital"},
                                            {"label": " Facility Reports",  "value": "facility"}],
                                   value="hospital", inline=True, className="mt-1"),
                ], md=4),
                dbc.Col([dbc.Label("Month", style=_LBL),
                         dcc.Dropdown(id="rpt-month", options=_MONTH_OPTS,
                                      value=def_month, clearable=False, style=_DD)], md=3),
                dbc.Col([dbc.Label("Year", style=_LBL),
                         dcc.Dropdown(id="rpt-year", options=_YEAR_OPTS,
                                      value=def_year, clearable=False, style=_DD)], md=2),
                dbc.Col([
                    dbc.Label("Bulk select", style=_LBL),
                    dbc.Row([
                        dbc.Col(dbc.Button("Select All", id="rpt-sel-all", size="sm",
                                           color="outline-secondary", className="me-1"),
                                width="auto"),
                        dbc.Col(dbc.Button("Clear", id="rpt-sel-none", size="sm",
                                           color="outline-secondary"),
                                width="auto"),
                    ], className="mt-1 g-0"),
                ], md=3),
            ], className="g-3 mb-3"),

            dbc.Row([dbc.Col([
                dbc.Label("Select Hospitals / Facilities", style=_LBL),
                dcc.Dropdown(id="rpt-targets", options=[], multi=True,
                             placeholder="Loading…", style={"fontSize": "0.82rem"}),
            ])], className="mb-3"),

            html.Div([
                dbc.Row([
                    dbc.Col(dbc.Checklist(
                        id="rpt-send-email",
                        options=[{"label": " Send via email", "value": "send"}],
                        value=[], inline=True), width="auto"),
                    dbc.Col(dbc.Checklist(
                        id="rpt-dry-run",
                        options=[{"label": " Dry-run (redirect all to test address)", "value": "dry"}],
                        value=["dry"], inline=True), width="auto"),
                    dbc.Col(dbc.Checklist(
                        id="rpt-send-overview", style={} if role == "ministry" else {"display": "none"},
                        options=[{"label": " Also send National Overview PDF to senior officials",
                                  "value": "overview"}],
                        value=[], inline=True), width="auto"),
                ], className="g-3"),
            ], style={"display": "block" if can_email else "none"}, className="mb-3"),

            dbc.Button([html.I(className="bi bi-play-fill me-1"), "Generate Reports"],
                       id="rpt-start", color="danger", className="fw-semibold"),
        ]), className="border-0 shadow-sm mb-3", style={"borderRadius": "8px"}),

        html.Div(id="rpt-progress-area"),
        html.Div(id="rpt-results-area"),

        dcc.Store(id="rpt-job-id"),
        dcc.Interval(id="rpt-poll", interval=2000, disabled=True, n_intervals=0),
    ])


def register_callbacks(app) -> None:

    @app.callback(Output("rpt-targets", "options"),
                  Output("rpt-targets", "placeholder"),
                  Input("rpt-type", "value"))
    def _load_targets(rtype):
        from flask import session
        from data import get_df, filter_by_user
        user = session.get("user")
        if not user:
            return [], "Sign in first"
        df = get_df()
        if df is None or df.empty:
            return [], "No data"
        df  = filter_by_user(df, user)
        col = "district_hospital" if rtype == "hospital" else "health_facility"
        if col not in df.columns:
            return [], f"Column '{col}' not found"
        names = sorted(df[col].dropna().unique().tolist())
        lbl   = "hospitals" if rtype == "hospital" else "facilities"
        return [{"label": n, "value": n} for n in names], \
               f"Select {lbl} ({len(names)} available)…"

    @app.callback(Output("rpt-targets", "value"),
                  Input("rpt-sel-all",  "n_clicks"),
                  Input("rpt-sel-none", "n_clicks"),
                  State("rpt-targets",  "options"),
                  prevent_initial_call=True)
    def _sel_all(n_all, n_none, options):
        if ctx.triggered_id == "rpt-sel-all":
            return [o["value"] for o in (options or [])]
        return []

    @app.callback(
        Output("rpt-job-id",        "data"),
        Output("rpt-poll",          "disabled"),
        Output("rpt-progress-area", "children"),
        Output("rpt-start",         "disabled"),
        Input("rpt-start",          "n_clicks"),
        State("rpt-type",           "value"),
        State("rpt-targets",        "value"),
        State("rpt-month",          "value"),
        State("rpt-year",           "value"),
        State("rpt-send-email",     "value"),
        State("rpt-dry-run",        "value"),
        State("rpt-send-overview",  "value"),
        prevent_initial_call=True,
    )
    def _start(n, rtype, targets, month, year, email_chk, dry_chk, overview_chk):
        from flask import session
        import jobs
        from auth import has_min_role
        from data import get_df, filter_by_user
        user = session.get("user")
        if not user:
            return no_update, True, _alert("Not authenticated.", "danger"), False
        if user.get("readonly") or not has_min_role(user, "hospital"):
            return no_update, True, _alert("Reports are available to hospital level and above.",
                                           "danger"), False
        if not targets:
            return no_update, True, _alert("Select at least one target.", "warning"), False
        # The target list comes from the browser — keep only hospitals/facilities
        # inside this user's own area (same source as the dropdown).
        df  = filter_by_user(get_df(), user)
        col = "district_hospital" if rtype == "hospital" else "health_facility"
        allowed = set(df[col].dropna()) if df is not None and col in df.columns else set()
        outside = [t for t in targets if t not in allowed]
        targets = [t for t in targets if t in allowed]
        if outside:
            return no_update, True, _alert(f"Outside your area: {', '.join(outside[:5])}", "danger"), False
        # Emailing follows the page's rule; the national overview is ministry-only.
        if email_chk and user.get("role") not in ("ministry", "district", "hospital"):
            email_chk = []
        if overview_chk and user.get("role") != "ministry":
            overview_chk = []
        who = user.get("username", "unknown")
        if rtype == "hospital":
            jid = jobs.start_hospital_job(targets, month, year, bool(email_chk),
                                          who, dry_run=bool(dry_chk),
                                          send_overview=bool(overview_chk))
        else:
            jid = jobs.start_facility_job(targets, month, year, bool(email_chk),
                                          who, dry_run=bool(dry_chk),
                                          send_overview=bool(overview_chk))
        prog = html.Div([
            html.Div(f"Job started — {len(targets)} {rtype}(s)…",
                     className="text-muted mb-2", style={"fontSize": "0.83rem"}),
            dbc.Progress(id="rpt-bar", value=0, label="0%", color="danger",
                         striped=True, animated=True, style={"height": "18px"}),
            html.Div(id="rpt-task-text", className="text-muted mt-1",
                     style={"fontSize": "0.75rem"}),
        ], className="mb-3")
        return jid, False, prog, True

    @app.callback(
        Output("rpt-bar",           "value"),
        Output("rpt-bar",           "label"),
        Output("rpt-bar",           "animated"),
        Output("rpt-task-text",     "children"),
        Output("rpt-poll",          "disabled", allow_duplicate=True),
        Output("rpt-results-area",  "children"),
        Output("rpt-start",         "disabled", allow_duplicate=True),
        Input("rpt-poll",           "n_intervals"),
        State("rpt-job-id",         "data"),
        prevent_initial_call=True,
    )
    def _poll(_, jid):
        import jobs
        if not jid:
            return 0, "", False, "", True, no_update, False
        j = jobs.get_job(jid)
        if not j:
            return 0, "", False, "Job not found.", True, no_update, False
        total    = j["total"] or 1
        progress = j["progress"]
        pct      = min(100, int(progress / total * 100))
        done     = j["status"] in ("completed", "failed")
        results  = _results_ui(j) if done else no_update
        return pct, f"{pct}%", not done, j["task"], done, results, not done


def _alert(msg: str, color: str = "info") -> dbc.Alert:
    return dbc.Alert(msg, color=color, dismissable=True, className="py-2 mb-3")


def _results_ui(job: dict) -> html.Div:
    results = job.get("results", [])
    errors  = job.get("errors",  [])
    ok      = job["status"] == "completed"
    header  = (f"✅ Completed — {len(results)} report(s)" if ok
               else f"❌ Failed — {job.get('task','')}")

    rows = []
    for r in results:
        name  = r.get("hospital") or r.get("facility", "—")
        stat  = r.get("status", "—")
        cases = r.get("total_cases", "—")
        emld  = "✅" if r.get("email_sent") else ("❌" if "email_error" in r else "—")
        note  = r.get("error") or r.get("reason") or r.get("email_error") or ""
        bc    = "success" if stat == "generated" else "warning" if stat == "skipped" else "danger"
        rows.append(html.Tr([
            html.Td(name,  style={"fontSize": "0.8rem"}),
            html.Td(dbc.Badge(stat, color=bc)),
            html.Td(f"{cases:,}" if isinstance(cases, int) else cases,
                    style={"textAlign": "right", "fontSize": "0.8rem"}),
            html.Td(emld, style={"textAlign": "center"}),
            html.Td(note, style={"fontSize": "0.72rem", "color": "#E74C3C"}),
        ]))

    tbl = dbc.Table([
        html.Thead(html.Tr([html.Th("Name"), html.Th("Status"),
                            html.Th("Cases", style={"textAlign": "right"}),
                            html.Th("Email", style={"textAlign": "center"}),
                            html.Th("Note")])),
        html.Tbody(rows),
    ], bordered=True, hover=True, size="sm", className="mb-0")

    err_block = (dbc.Alert([
        html.Strong("Worker errors:"),
        html.Pre("\n".join(errors[-3:]),
                 style={"fontSize": "0.7rem", "maxHeight": "200px",
                        "overflowY": "auto", "margin": 0}),
    ], color="danger", className="mt-2 py-2") if errors else html.Div())

    return html.Div([
        dbc.Alert(header, color="success" if ok else "danger", className="py-2 mb-2"),
        dbc.Card(dbc.CardBody(tbl, className="p-0"),
                 className="border-0 shadow-sm", style={"borderRadius": "8px"}),
        err_block,
    ])
