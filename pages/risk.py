"""
pages/risk.py — At-Risk Children · Missed Appointments · Sync · Pipeline Guide

Tabs:
  1. At-Risk List     — run growth-velocity classifier, filter, email alerts
  2. Missed Appts     — children 10+ days past their scheduled next_visit_date
  3. Sync eTracker    — manual-only button to pull data from the API
  4. Pipeline Guide   — explains the full pipeline and data-cleaning steps

Key design decisions:
  • layout() does NOT call get_df() — no blocking calls at render time.
    Data is loaded only when the user clicks "Run Classifier" or "Load".
  • sync-progress-area lives in the permanent layout (outside tab content)
    so the poll callback can always update it without "nonexistent object" errors.
  • dcc.Interval starts disabled=True and is only enabled after "Start Sync".
  • All PostgreSQL connections use connect_timeout=5 (config/db_local.py).
  • entity_id is used for deduplication throughout (not child_id).
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from dash import Input, Output, State, dcc, html, no_update
import dash_bootstrap_components as dbc

_LBL  = {"fontSize": "0.78rem", "fontWeight": "600", "color": "#5D6D7E"}
_DD   = {"fontSize": "0.82rem"}
_RED  = "#C0392B"
_GREY = "#6C757D"


# ── Small helpers ──────────────────────────────────────────────────────────────

def _card_wrap(content, **style) -> dbc.Card:
    return dbc.Card(
        dbc.CardBody(content, className="p-2"),
        className="border-0 shadow-sm mb-3",
        style={"borderRadius": "8px", **style},
    )


# ── Risk table ─────────────────────────────────────────────────────────────────

def _missed_table(df: pd.DataFrame) -> dbc.Table:
    cols = [
        ("entity_id",         "Entity ID"),
        ("child_name",        "Name"),
        ("gender",            "Gender"),
        ("age_in_months",     "Age (mo)"),
        ("health_facility",   "Facility"),
        ("district_hospital", "Hospital"),
        ("district",          "District"),
        ("next_visit_date",   "Next Visit"),
        ("days_overdue",      "Days Overdue"),
        ("mother_phone",      "Mother Phone"),
        ("father_phone",      "Father Phone"),
    ]
    avail = [(s, h) for s, h in cols if s in df.columns]
    header = html.Thead(html.Tr(
        [html.Th(h, style={"fontSize": "0.75rem", "whiteSpace": "nowrap"})
         for _, h in avail]
    ))

    def _fmt(col, val):
        if pd.isna(val) if not isinstance(val, (list, str)) else (not val):
            return "—"
        if col == "next_visit_date":
            try:
                return pd.Timestamp(val).strftime("%d %b %Y")
            except Exception:
                return str(val)
        if col == "days_overdue":
            try:
                d = int(val)
                color = "#C0392B" if d > 30 else "#E67E22"
                return html.Span(f"{d}d", style={"color": color, "fontWeight": "600"})
            except Exception:
                return str(val)
        if col in ("age_in_months",):
            try:
                return f"{float(val):.0f}"
            except Exception:
                return str(val)
        return str(val)

    rows = []
    for _, row in df.iterrows():
        d = row.get("days_overdue", 0) or 0
        bg = "#FDF2F2" if d > 30 else "#FFF8F0"
        rows.append(html.Tr(
            [html.Td(_fmt(col, row.get(col)), style={"fontSize": "0.76rem"})
             for col, _ in avail],
            style={"background": bg},
        ))

    return dbc.Table([header, html.Tbody(rows)],
                     bordered=True, hover=True, size="sm",
                     responsive=True, className="mb-0")


# ── Layout ─────────────────────────────────────────────────────────────────────

def layout(user: dict) -> html.Div:
    """
    NOTE: This function does NOT call get_df() or any blocking operation.
    Data is loaded only when the user explicitly clicks a button.
    This prevents blocking the Dash server thread at page render time.
    """
    return html.Div([
        html.H4("At-Risk Children", className="nhic-page-title mb-2"),

        dbc.Tabs(
            [dbc.Tab(label="Dashboard",      tab_id="risk-dash")]
            # child lists + operations need a login
            + ([] if user.get("public") else [dbc.Tab(label="Missed Appts", tab_id="missed")])
            # eTracker sync is operational — hidden for view-only link users.
            # eTracker sync is a national operation — ministry administrators only.
            + ([dbc.Tab(label="Sync eTracker", tab_id="sync")]
               if user.get("role") == "ministry" and not user.get("readonly") else [])
            + [dbc.Tab(label="Pipeline Guide", tab_id="guide")],
            id="risk-tabs", active_tab="risk-dash", className="nhic-tabs mb-3"),

        html.Div(id="risk-tab-content"),

        # ── Always-in-DOM elements ────────────────────────────────────────────
        # sync-progress-area is here (NOT inside tab content) so the poll
        # callback can always update it regardless of which tab is active.
        html.Div(id="sync-progress-area", className="mt-3"),

        dcc.Store(id="risk-job-id"),
        # Interval starts DISABLED. It is only enabled when Start Sync is clicked.
        dcc.Interval(id="risk-poll", interval=4000, disabled=True, n_intervals=0),
    ])


# ── Tab content builders ───────────────────────────────────────────────────────

def _missed_tab(user: dict) -> html.Div:
    """Missed appointments tab — children 10+ days overdue on next_visit_date."""
    role   = user.get("role", "")
    locked = role in ("hospital", "health_center")

    return html.Div([
        _card_wrap([
            dbc.Row([
                dbc.Col([
                    dbc.Label("District", style=_LBL),
                    dcc.Dropdown(id="missed-district", options=[],
                                 placeholder="All Districts",
                                 disabled=locked, clearable=True, style=_DD),
                ], md=3),
                dbc.Col([
                    dbc.Label("Days Overdue (min)", style=_LBL),
                    dcc.Slider(id="missed-threshold", min=10, max=90, step=5,
                               value=10, marks={10: "10d", 30: "30d",
                                                60: "60d", 90: "90d"},
                               tooltip={"placement": "bottom"}),
                ], md=5),
                dbc.Col([
                    dbc.Label(" ", style=_LBL),
                    dbc.Button(
                        [html.I(className="bi bi-search me-1"), "Load Missed Appointments"],
                        id="missed-load-btn", color="warning", size="sm",
                        className="fw-semibold d-block mt-1",
                    ),
                ], md=4),
            ], className="g-2"),
        ]),
        dbc.Row(id="missed-kpi-row", className="g-2 mb-3"),
        html.Div(id="missed-results"),
    ])


def _sync_tab() -> html.Div:
    """Manual-only sync tab. No auto-sync. Sync only runs on button click."""
    return html.Div([
        dbc.Alert(
            [html.I(className="bi bi-hand-index-thumb-fill me-2"),
             html.Strong("Manual sync only. "),
             "Data is fetched from the eTracker API only when you click "
             "'Start Sync' below. The dashboard does not auto-sync in the background."],
            color="info", className="mb-3 py-2", style={"fontSize": "0.82rem"},
        ),
        _card_wrap([
            html.H6("Sync from eTracker API", className="fw-bold mb-3",
                    style={"color": "#2C3E50"}),
            dbc.Row([
                dbc.Col([
                    dbc.Label("Sync Mode", style=_LBL),
                    dbc.RadioItems(
                        id="sync-mode",
                        options=[
                            {"label": " Incremental — fetch new/updated records only (fast, ~5–10 min)",
                             "value": "sync"},
                            {"label": " Full initial load — fetch all 30 districts from scratch (slow, ~60–90 min)",
                             "value": "initial"},
                        ],
                        value="sync", className="mt-1",
                    ),
                ], md=8),
                dbc.Col([
                    dbc.Label(" ", style=_LBL),
                    dbc.Button(
                        [html.I(className="bi bi-cloud-download me-1"), "Start Sync"],
                        id="sync-start-btn", color="danger",
                        className="fw-semibold d-block mt-1",
                    ),
                ], md=4),
            ], className="g-3"),
            html.Hr(className="my-3"),
            dbc.Row([
                dbc.Col([
                    dbc.Label("Or fetch a specific date range from the API", style=_LBL),
                    dcc.DatePickerRange(
                        id="sync-range", display_format="YYYY-MM-DD",
                        className="d-block mt-1", style={"fontSize": "0.82rem"}),
                ], md=8),
                dbc.Col([
                    dbc.Label(" ", style=_LBL),
                    dbc.Button(
                        [html.I(className="bi bi-calendar-range me-1"), "Fetch Range"],
                        id="sync-range-btn", color="danger", outline=True,
                        className="fw-semibold d-block mt-1"),
                ], md=4),
            ], className="g-3"),
            dbc.Checklist(
                id="sync-force-refetch",
                options=[{"label": " Force full re-fetch (ignore previously-completed "
                                    "progress for this exact date range)",
                          "value": "force"}],
                value=[], className="mt-2",
                style={"fontSize": "0.8rem"},
            ),
            dbc.Alert(
                [html.I(className="bi bi-info-circle me-2"),
                 "By default, re-fetching the same date range skips districts/pages "
                 "already synced (fast resume after an interruption). Check the box above "
                 "if you specifically need to re-check a past window for late-arriving "
                 "or corrected records — for ongoing new-data pickup, use Incremental "
                 "Sync above instead."],
                color="light", className="py-2 mt-2 mb-0", style={"fontSize": "0.78rem"},
            ),
            html.Hr(className="my-3"),
            dbc.Alert(
                [html.I(className="bi bi-info-circle me-2"),
                 "Sync progress appears below. Dashboard data refreshes automatically "
                 "when the sync completes. The interval stops as soon as the job finishes."],
                color="light", className="py-2 mb-0", style={"fontSize": "0.80rem"},
            ),
        ]),
    ])


def _pipeline_guide_tab() -> html.Div:
    """Step-by-step pipeline guide including data-cleaning rules."""

    def _step(n, title, icon, color, body) -> dbc.Card:
        return dbc.Card(dbc.CardBody([
            dbc.Row([
                dbc.Col(dbc.Badge(str(n), color=color, pill=True,
                                  style={"fontSize": "1rem", "width": "32px",
                                         "height": "32px", "lineHeight": "26px",
                                         "textAlign": "center"}), width="auto"),
                dbc.Col(html.Div([
                    html.Div([html.I(className=f"bi {icon} me-2"),
                              html.Strong(title)],
                             style={"color": "#2C3E50", "marginBottom": "6px"}),
                    body,
                ])),
            ], className="g-3 align-items-start"),
        ]), className="border-0 shadow-sm mb-3", style={"borderRadius": "8px"})

    cleaning_rules = dbc.ListGroup([
        dbc.ListGroupItem([html.Strong("Weight: "), "1.5 – 35 kg   (outside → NaN, not deleted)"]),
        dbc.ListGroupItem([html.Strong("Height: "), "40 – 130 cm   (outside → NaN)"]),
        dbc.ListGroupItem([html.Strong("Age: "),    "0 – 72 months (outside → NaN)"]),
        dbc.ListGroupItem([html.Strong("HAZ z-score: "), "±6 SD   (WHO biologically implausible flag → NaN)"]),
        dbc.ListGroupItem([html.Strong("Weight velocity: "), "absolute change > 3 kg/month → NaN (data-entry error)"]),
        dbc.ListGroupItem([html.Strong("Height velocity: "), "absolute change > 10 cm/month → NaN"]),
    ], flush=True, style={"fontSize": "0.82rem"})

    who_rules = dbc.ListGroup([
        dbc.ListGroupItem([html.Strong("HIGH: "), "weight loss between visits (velocity < 0)  |  MUAC < 11.5 cm  |  severe wasting"]),
        dbc.ListGroupItem([html.Strong("MEDIUM: "), "weight velocity < 50% of WHO age-band minimum  |  height velocity < 30% of expected  |  MUAC 11.5–12.5 cm  |  moderate wasting"]),
        dbc.ListGroupItem("Age at each visit is calculated from date_of_birth + visit_date (not the recorded age_in_months field)."),
        dbc.ListGroupItem("WHO velocity norms: 0–3 mo 0.50 kg | 3–6 mo 0.30 kg | 6–9 mo 0.18 kg | 9–12 mo 0.13 kg | 12–24 mo 0.08 kg | 24–36 mo 0.05 kg | 36–60 mo 0.04 kg"),
    ], flush=True, style={"fontSize": "0.82rem"})

    epi_rules = dbc.ListGroup([
        dbc.ListGroupItem("Vaccine is flagged MISSED when: child's age ≥ due_window_end AND recorded value is not 'Yes' / '1'."),
        dbc.ListGroupItem("Only the latest visit per child is used (entity_id deduplication)."),
        dbc.ListGroupItem("Schedule: BCG/OPV/HepB at birth  →  DPT+PCV+Rota at 6,10,14 weeks  →  MR1 at 9 months  →  MR2 at 15 months."),
    ], flush=True, style={"fontSize": "0.82rem"})

    return html.Div([
        _step(1, "Fetch Data from eTracker API", "bi-cloud-download-fill", "primary", html.Div([
            html.P("Go to the Sync eTracker tab → choose Incremental or Full → click Start Sync.",
                   className="mb-1", style={"fontSize": "0.82rem"}),
            html.P("The sync fetches all 30 districts in parallel threads, paging through the "
                   "DHIS2 tracker analytics endpoint in monthly chunks. Results are written to "
                   "immunization_vaccination in your local PostgreSQL (immunization_db).",
                   className="mb-0", style={"fontSize": "0.82rem"}),
        ])),
        _step(2, "Data Cleaning (automatic on load)", "bi-funnel-fill", "warning", html.Div([
            html.P("Applied automatically every time the data is loaded from PostgreSQL or CSV:",
                   className="mb-2", style={"fontSize": "0.82rem"}),
            cleaning_rules,
            html.P("Flagged values are set to NaN so they don't distort averages or velocities. "
                   "Original rows are never deleted from the database.",
                   className="mt-2 mb-0", style={"fontSize": "0.82rem"}),
        ])),
        _step(3, "Growth Velocity Risk Classification", "bi-cpu-fill", "danger", html.Div([
            html.P("Go to the At-Risk List tab → click Run Classifier.",
                   className="mb-2", style={"fontSize": "0.82rem"}),
            who_rules,
            html.P("Each child gets one risk level (worst across all visits). "
                   "Children with only one visit are flagged by MUAC / wasting status only "
                   "(no velocity data).",
                   className="mt-2 mb-0", style={"fontSize": "0.82rem"}),
        ])),
        _step(4, "Missed Appointments", "bi-calendar-x-fill", "warning", html.Div([
            html.P("Go to the Missed Appts tab → set Days Overdue threshold (default 10) → click Load.",
                   className="mb-1", style={"fontSize": "0.82rem"}),
            html.P("A missed appointment is: today − next_visit_date > threshold. "
                   "Only the latest visit per child (entity_id) is checked, so a "
                   "child who came to a later visit is not flagged.",
                   className="mb-0", style={"fontSize": "0.82rem"}),
        ])),
        _step(5, "Vaccination Coverage", "bi-shield-fill-check", "success", html.Div([
            html.P("Visible in Dashboard → Vaccination tab (auto-computed from loaded data).",
                   className="mb-2", style={"fontSize": "0.82rem"}),
            epi_rules,
        ])),
        _step(6, "Reports & Email Alerts", "bi-envelope-fill", "secondary", html.Div([
            html.P("Reports: Dashboard → Reports tab → select child → Generate PDF.",
                   className="mb-1", style={"fontSize": "0.82rem"}),
            html.P("At-risk alerts: At-Risk List tab → Run Classifier → Email Alerts. "
                   "Emails are sent per facility to nsanzabandidani@gmail.com (test mode).",
                   className="mb-0", style={"fontSize": "0.82rem"}),
        ])),
    ])


# ── Callbacks ──────────────────────────────────────────────────────────────────

def register_callbacks(app) -> None:

    # ── Tab routing ────────────────────────────────────────────────────────────
    @app.callback(Output("risk-tab-content", "children"),
                  Input("risk-tabs", "active_tab"), State("url", "search"))
    def _switch(tab, search):
        from auth import current_user
        user = (current_user() or {})
        if tab == "sync" and user.get("role") == "ministry" and not user.get("readonly"):
            return _sync_tab()
        if tab == "guide":
            return _pipeline_guide_tab()
        if user.get("public") and tab not in ("risk-dash", "guide"):
            tab = "risk-dash"
        if tab == "missed":
            return _missed_tab(user)
        from pages.risk_dashboard import layout as risk_dashboard
        return risk_dashboard(user, search)

    @app.callback(
        Output("missed-district", "options"),
        Input("risk-tabs", "active_tab"),
        prevent_initial_call=True,
    )
    def _populate_missed_filters(tab):
        if tab != "missed":
            return no_update
        from auth import current_user
        from data import get_df, filter_by_user, district_options
        user = (current_user() or {})
        df   = get_df()
        if df is None or df.empty:
            return []
        return district_options(filter_by_user(df, user))

    # ── Load missed appointments ───────────────────────────────────────────────
    @app.callback(
        Output("missed-kpi-row", "children"),
        Output("missed-results", "children"),
        Input("missed-load-btn", "n_clicks"),
        State("missed-district",  "value"),
        State("missed-threshold", "value"),
        prevent_initial_call=True,
    )
    def _load_missed(n, district, threshold):
        from auth import current_user
        from data import get_df, filter_by_user, filter_geo, get_missed_appointments, nuniq
        from components.kpi import kpi_card, C_DANGER, C_WARNING, C_INFO, C_TEAL

        user = (current_user() or {})
        df   = get_df()
        if df is None or df.empty:
            return [], dbc.Alert("No data loaded. Sync from eTracker first.",
                                 color="warning")

        df = filter_by_user(df, user)
        if district:
            df = filter_geo(df, district=district)

        missed = get_missed_appointments(df, threshold_days=threshold or 10)

        if missed.empty:
            return [], dbc.Alert(
                f"No children are more than {threshold or 10} days past their scheduled visit.",
                color="success", className="mt-2")

        n_total   = len(missed)
        n_30plus  = int((missed["days_overdue"] > 30).sum())
        n_dist    = int(missed["district"].nunique()) if "district" in missed.columns else 0
        n_fac     = int(missed["health_facility"].nunique()) if "health_facility" in missed.columns else 0

        kpis = dbc.Row([
            kpi_card("Missed Appointments", str(n_total),  f">{threshold or 10}d overdue",
                     C_DANGER,  "bi-calendar-x-fill"),
            kpi_card(">30 Days Overdue",    str(n_30plus), "highest priority",
                     "#8B0000",  "bi-exclamation-circle-fill"),
            kpi_card("Districts Affected",  str(n_dist),   "districts",
                     C_INFO,    "bi-geo-alt-fill"),
            kpi_card("Facilities",          str(n_fac),    "facilities",
                     C_WARNING, "bi-hospital-fill"),
        ], className="g-2")

        tbl  = _missed_table(missed.head(300))
        note = (html.Div(f"Showing first 300 of {n_total:,} overdue children.",
                         className="text-muted mt-1 mb-2",
                         style={"fontSize": "0.75rem"})
                if n_total > 300 else html.Div())

        chart = _missed_chart(missed)
        results = html.Div([
            chart, note,
            dbc.Card(dbc.CardBody(tbl, className="p-0"),
                     className="border-0 shadow-sm", style={"borderRadius": "8px"}),
        ])

        return kpis, results

    # ── Start sync (mode buttons OR custom date range) ─────────────────────────
    @app.callback(
        Output("risk-job-id",        "data"),
        Output("risk-poll",          "disabled"),
        Output("sync-progress-area", "children"),
        Input("sync-start-btn",      "n_clicks"),
        Input("sync-range-btn",      "n_clicks"),
        State("sync-mode",           "value"),
        State("sync-range",          "start_date"),
        State("sync-range",          "end_date"),
        State("sync-force-refetch",  "value"),
        prevent_initial_call=True,
    )
    def _start_sync(n_mode, n_range, mode, r_start, r_end, force_refetch):
        from dash import ctx
        from auth import current_user
        # Only ministry administrators may trigger a (national) sync.
        _u = current_user() or {}
        if _u.get("readonly") or _u.get("role") != "ministry":
            return no_update, no_update, no_update
        # Guard: only act on a real button click. When the Sync tab is opened,
        # the buttons are (re)created with n_clicks=None, which can trigger this
        # callback — ignore that so a sync never starts on its own.
        if not n_mode and not n_range:
            return no_update, no_update, no_update
        if ctx.triggered_id not in ("sync-start-btn", "sync-range-btn"):
            return no_update, no_update, no_update
        try:
            from sync.etl import start_sync_job, clear_sync_progress
            if ctx.triggered_id == "sync-range-btn":
                if not r_start or not r_end:
                    return no_update, True, dbc.Alert(
                        "Pick both a start and end date first.",
                        color="warning", className="py-2 mt-2")
                force = bool(force_refetch and "force" in force_refetch)
                if force:
                    n_cleared = clear_sync_progress("range", r_start, r_end)
                    _log_note = f" (forced full re-fetch, {n_cleared} prior progress row(s) cleared)"
                else:
                    _log_note = ""
                jid = start_sync_job(mode="range", start_date=r_start, end_date=r_end,
                                     resume=not force)
                label = f"Fetching {r_start} → {r_end} — 30 districts…{_log_note}"
            else:
                jid = start_sync_job(mode=mode or "sync")
                label = f"Sync started ({mode} mode) — 30 districts…"
            prog = _progress_ui(0, 0, 30, [], running=True, label=label)
            return jid, False, prog
        except Exception as exc:
            return None, True, dbc.Alert(f"Failed to start sync: {exc}",
                                          color="danger", className="py-2 mt-2")

    # ── Poll sync progress ─────────────────────────────────────────────────────
    # Outputs ONLY to sync-progress-area (always in DOM) and risk-poll.disabled.
    # Never targets nested sub-elements that may not exist.
    @app.callback(
        Output("sync-progress-area", "children", allow_duplicate=True),
        Output("risk-poll",          "disabled",  allow_duplicate=True),
        Input("risk-poll",           "n_intervals"),
        State("risk-job-id",         "data"),
        prevent_initial_call=True,
    )
    def _poll_sync(_, jid):
        if not jid:
            return no_update, True   # disable interval immediately if no job

        try:
            from sync.etl import get_sync_job
        except Exception as exc:
            return dbc.Alert(f"Import error: {exc}", color="danger",
                             className="py-2 mt-2"), True

        try:
            job = get_sync_job(jid)
            if not job:
                return no_update, True

            done_count  = len(job.get("totals", {}))
            total_rows  = sum(job.get("progress", {}).values())
            active_dist = [d for d in job.get("progress", {})
                           if d not in job.get("totals", {})]
            done        = job["status"] in ("completed", "failed")

            # Dashboard caches are refreshed by the sync job itself
            # (sync/etl.py → data.refresh_after_sync), incrementally.

            prog = _progress_ui(
                done_count, total_rows, 30, active_dist,
                running=not done,
                failed=job["status"] == "failed",
                error=job.get("error"),
            )
            return prog, done
        except Exception as exc:
            return dbc.Alert(f"Poll error: {exc}", color="danger",
                             className="py-2 mt-2"), True


# ── Progress UI ────────────────────────────────────────────────────────────────

def _progress_ui(done: int, rows: int, total: int,
                 active: list[str],
                 running: bool = True,
                 failed: bool  = False,
                 error: str | None = None,
                 label: str = "") -> html.Div:
    if not label:
        label = (f"Sync complete — {done}/{total} districts, {rows:,} rows"
                 if not running and not failed
                 else f"{done}/{total} districts · {rows:,} rows fetched")

    pct   = min(100, int(done / max(total, 1) * 100))
    color = "danger" if not failed else "warning"

    active_txt = (html.Div(f"Active: {', '.join(active[:5])}",
                           style={"fontSize": "0.75rem", "color": "#5D6D7E",
                                  "marginTop": "4px"})
                  if active else html.Div())

    err_block = (dbc.Alert(f"Sync error: {error}", color="danger",
                           className="py-2 mt-2")
                 if failed and error else html.Div())
    ok_block  = (dbc.Alert("✅ Sync complete — dashboard data refreshed.",
                            color="success", className="py-2 mt-2")
                 if not running and not failed else html.Div())

    return dbc.Card(dbc.CardBody([
        html.Div(label, className="text-muted mb-2", style={"fontSize": "0.83rem"}),
        dbc.Progress(value=pct, label=f"{pct}%", color=color,
                     striped=running, animated=running, style={"height": "18px"}),
        active_txt, err_block, ok_block,
    ]), className="border-0 shadow-sm", style={"borderRadius": "8px"})


# ── Chart helpers ──────────────────────────────────────────────────────────────

_BG   = "rgba(0,0,0,0)"
_FONT = dict(family="Inter,system-ui,sans-serif", size=11)
_MAR  = dict(l=40, r=20, t=36, b=40)
_H    = 300


def _missed_chart(df: pd.DataFrame) -> html.Div:
    if "district" not in df.columns or "days_overdue" not in df.columns:
        return html.Div()

    grp = (df.groupby("district")
             .agg(children=("days_overdue", "count"),
                  avg_days=("days_overdue", "mean"))
             .reset_index()
             .sort_values("children", ascending=True)
             .tail(15))

    fig = px.bar(grp, x="children", y="district", orientation="h",
                 color="avg_days",
                 color_continuous_scale=["#FFF3CD", "#C0392B"],
                 title="Missed Appointments by District (top 15)",
                 labels={"children": "Children", "district": "",
                         "avg_days": "Avg Days Overdue"},
                 hover_data={"avg_days": ":.0f"})
    fig.update_layout(paper_bgcolor=_BG, plot_bgcolor=_BG, font=_FONT,
                      margin=_MAR, height=max(300, len(grp) * 40 + 60))

    return dbc.Card(dbc.CardBody(
        dcc.Graph(figure=fig, config={"displayModeBar": False},
                  style={"height": f"{max(300, len(grp)*40+60)}px"}),
        className="p-2"),
        className="border-0 shadow-sm mb-3", style={"borderRadius": "8px"})
