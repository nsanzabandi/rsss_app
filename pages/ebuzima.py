"""
pages/ebuzima.py — eBuzima Nutrition Analytics (ported from the standalone
ebuzima_nutrition project as a new page inside RSSS).

Read-only analytics over eBuzima MoH data (separate PostgreSQL + ClickHouse
from rsss_app's own eTracker/immunization_db pipeline — see ebuzima/db.py
and ebuzima/clickhouse_source.py).

Key design decisions (mirrors pages/risk.py):
  • layout() does NOT call ensure_loaded() — no blocking calls at render time.
    Data loads lazily on first callback invocation (ensure_loaded() inside
    ebuzima/analytics.py), the same principle rsss_app's own pages/risk.py
    documents for get_df().
  • "Sync Data" is manual-only (button click), matching rsss_app's own
    explicit choice for the eTracker sync in pages/risk.py — no background
    schedule added here either.
  • All component ids are namespaced with an "eb-" prefix so they can never
    collide with ids in pages/dashboard.py, pages/followup.py, pages/risk.py,
    or pages/reports.py.
  • This page does not replace or modify pages/followup.py in any way — it is
    purely additive.
"""
from __future__ import annotations

import threading

import pandas as pd
from dash import Input, Output, ctx, dcc, html, no_update
import dash_bootstrap_components as dbc

from ebuzima import analytics as eb

_LBL  = {"fontSize":"11px","fontWeight":"700","color":eb.C["muted"],"marginBottom":"3px",
          "textTransform":"uppercase","letterSpacing":"0.4px"}
_DD   = {"fontSize":"13px","marginBottom":"9px"}


# ── Layout ─────────────────────────────────────────────────────────────────────

def layout(user: dict) -> html.Div:
    """
    NOTE: This function does NOT call ensure_loaded() or any blocking operation.
    Data is loaded lazily inside the first callback that needs it.
    """
    readonly = bool(user.get("readonly"))

    return html.Div([
        dbc.Row([
            dbc.Col(html.H5("eBuzima Nutrition Analytics",
                            className="fw-bold mb-0", style={"color": "#2C3E50"})),
            dbc.Col(dbc.Badge("Data source — eBuzima MoH", color="secondary",
                              className="fw-semibold ms-2"),
                    width="auto", className="d-flex align-items-center"),
        ], className="align-items-center mb-3"),

        dbc.Card(dbc.CardBody([
            dbc.Row([
                dbc.Col([dbc.Label("Dataset", style=_LBL),
                         dcc.Dropdown(id="eb-dd-ds", options=[], value=None,
                                      clearable=False, style=_DD)], md=2),
                dbc.Col([dbc.Label("Province", style=_LBL),
                         dcc.Dropdown(id="eb-dd-prov", options=[], multi=True,
                                      placeholder="All provinces", style=_DD)], md=2),
                dbc.Col([dbc.Label("District", style=_LBL),
                         dcc.Dropdown(id="eb-dd-dist", options=[], multi=True,
                                      placeholder="All", style=_DD)], md=2),
                dbc.Col([dbc.Label("Sector", style=_LBL),
                         dcc.Dropdown(id="eb-dd-sec", options=[], multi=True,
                                      placeholder="All", style=_DD)], md=2),
                dbc.Col([dbc.Label("Health Center", style=_LBL),
                         dcc.Dropdown(id="eb-dd-hc", options=[], multi=True,
                                      placeholder="All", style=_DD)], md=2),
                dbc.Col([dbc.Label("Date range (creation)", style=_LBL),
                         dcc.DatePickerRange(id="eb-date-rng", display_format="YYYY-MM-DD",
                                              className="d-block", style={"fontSize": "0.8rem"})], md=2),
            ], className="g-2"),
        ]), className="border-0 shadow-sm mb-3", style={"borderRadius": "8px"}),

        html.Div(id="eb-filter-info", className="text-muted mb-2",
                 style={"fontSize": "0.78rem"}),

        dbc.Row(id="eb-kpi-row", className="g-2 mb-3"),

        dbc.Tabs(
            [dbc.Tab(label="Stunting & Malnutrition", tab_id="stunting"),
             dbc.Tab(label="Discharge & Outcomes",    tab_id="discharge"),
             dbc.Tab(label="Growth Monitoring",       tab_id="cgm"),
             dbc.Tab(label="Program Overview",        tab_id="overview"),
             dbc.Tab(label="Geography",               tab_id="geo"),
             dbc.Tab(label="Demographics",            tab_id="demo"),
             dbc.Tab(label="Data Quality",            tab_id="dq")],
            id="eb-tabs", active_tab="stunting", className="mb-3"),

        html.Div(id="eb-tab-content"),

        # ── Always-in-DOM elements ────────────────────────────────────────────
        html.Div(
            _sync_card() if not readonly else html.Div(),
            id="eb-sync-area", className="mt-3",
        ),

        # Fires exactly once at page load — decouples the initial dataset-options
        # load from tab switches (which must NOT reset the dataset selection).
        dcc.Store(id="eb-init", data=0),
        dcc.Store(id="eb-sync-done", data=0),
        dcc.Interval(id="eb-sync-poll", interval=2000, disabled=True, max_intervals=-1),
        # api-ping is a slow, low-priority health check — starts disabled and is
        # enabled once the tab first renders, so it never blocks page load.
        dcc.Interval(id="eb-api-ping", interval=5*60*1000, n_intervals=0),
    ])


def _sync_card() -> dbc.Card:
    return dbc.Card(dbc.CardBody([
        html.Div([
            html.H6("Sync from eBuzima (ClickHouse → PostgreSQL)", className="fw-bold mb-1",
                    style={"color": "#2C3E50"}),
            html.Span(id="eb-api-status", className="text-muted", style={"fontSize": "0.75rem"}),
        ], className="d-flex justify-content-between align-items-center"),
        dbc.Alert(
            [html.I(className="bi bi-hand-index-thumb-fill me-2"),
             html.Strong("Manual sync only. "),
             "Data is pulled from eBuzima's ClickHouse mirror only when you click "
             "'Sync Data' below — there is no background auto-sync for this page."],
            color="info", className="my-2 py-2", style={"fontSize": "0.8rem"},
        ),
        dbc.Button([html.I(className="bi bi-cloud-download me-1"), "Sync Data"],
                   id="eb-btn-sync", color="primary", size="sm", className="fw-semibold"),
        html.Div("Ready to sync", id="eb-sync-status", className="text-muted mt-2",
                 style={"fontSize": "0.78rem"}),
    ]), className="border-0 shadow-sm", style={"borderRadius": "8px"})


# ── Callbacks ──────────────────────────────────────────────────────────────────

def register_callbacks(app) -> None:

    # ── Populate the dataset dropdown + cascading geo filters on first load ────
    # Input is eb-init (fires exactly once at page load), NOT eb-tabs.active_tab —
    # switching tabs must not reset the user's dataset selection.
    @app.callback(
        Output("eb-dd-ds", "options"),
        Output("eb-dd-ds", "value"),
        Input("eb-init", "data"),
    )
    def _init_dataset(_):
        opts = eb.dataset_options()
        default = opts[0]["value"] if opts else None
        return opts, default

    @app.callback(
        Output("eb-dd-prov", "options"),
        Input("eb-dd-ds", "value"),
    )
    def _prov_opts(ds):
        if not ds:
            return []
        df = eb.active_df(ds)
        if df.empty or "province" not in df.columns:
            return []
        return eb.opts(df["province"])

    @app.callback(
        Output("eb-dd-dist", "options"),
        Input("eb-dd-ds", "value"), Input("eb-dd-prov", "value"),
    )
    def _dist_opts(ds, provs):
        if not ds:
            return []
        df = eb.active_df(ds)
        if df.empty or "district" not in df.columns:
            return []
        if provs and "province" in df.columns:
            df = df[df["province"].isin(provs)]
        return eb.opts(df["district"])

    @app.callback(
        Output("eb-dd-sec", "options"),
        Input("eb-dd-ds", "value"), Input("eb-dd-prov", "value"), Input("eb-dd-dist", "value"),
    )
    def _sec_opts(ds, provs, dists):
        if not ds:
            return []
        df = eb.active_df(ds)
        if df.empty or "sector" not in df.columns:
            return []
        if provs and "province" in df.columns: df = df[df["province"].isin(provs)]
        if dists and "district" in df.columns: df = df[df["district"].isin(dists)]
        return eb.opts(df["sector"])

    @app.callback(
        Output("eb-dd-hc", "options"),
        Input("eb-dd-ds", "value"), Input("eb-dd-prov", "value"),
        Input("eb-dd-dist", "value"), Input("eb-dd-sec", "value"),
    )
    def _hc_opts(ds, provs, dists, secs):
        if not ds:
            return []
        df = eb.active_df(ds)
        if df.empty or "company" not in df.columns:
            return []
        if provs and "province" in df.columns: df = df[df["province"].isin(provs)]
        if dists and "district" in df.columns: df = df[df["district"].isin(dists)]
        if secs  and "sector"   in df.columns: df = df[df["sector"].isin(secs)]
        return eb.opts(df["company"])

    # ── API health-check ping ───────────────────────────────────────────────────
    @app.callback(Output("eb-api-status", "children"), Input("eb-api-ping", "n_intervals"))
    def _check_api(_):
        import requests as req
        try:
            r = req.get(
                f"{eb_api_base_url()}/api/method/frappe.client.get_count",
                params={"doctype": "Nutrition"},
                headers={"Authorization": f"token {eb_api_key()}:{eb_api_secret()}"},
                timeout=6,
            )
            if r.status_code == 200:
                return [html.Span("●", style={"color": "#2ca25f"}), " API Live"]
            return [html.Span("●", style={"color": eb.C['warning']}), f" API {r.status_code}"]
        except Exception:
            return [html.Span("●", style={"color": "#ff6b6b"}), " API Unreachable"]

    # ── Sync button + polling ───────────────────────────────────────────────────
    @app.callback(
        Output("eb-sync-status", "children"),
        Output("eb-sync-poll",   "disabled"),
        Output("eb-sync-done",   "data"),
        Input("eb-btn-sync",   "n_clicks"),
        Input("eb-sync-poll",  "n_intervals"),
        prevent_initial_call=True,
    )
    def _handle_sync(n_clicks, _poll):
        trigger = ctx.triggered_id

        if trigger == "eb-btn-sync":
            with eb._ingest_lock:
                if eb._ingest_state["running"]:
                    return "Sync already in progress…", False, no_update
                eb._ingest_state.update({
                    "running":  True,
                    "started":  pd.Timestamp.now().isoformat(),
                    "results":  None,
                    "finished": None,
                })
            threading.Thread(target=eb.run_ingest_bg, daemon=True).start()
            return "Starting sync…", False, no_update

        with eb._ingest_lock:
            state = dict(eb._ingest_state)

        if state["running"]:
            elapsed = int((pd.Timestamp.now() - pd.Timestamp(state["started"])).total_seconds())
            return f"Syncing… {elapsed}s", False, no_update

        if state["results"] is not None:
            inserted = sum(r.get("inserted", 0) for r in state["results"])
            updated  = sum(r.get("updated",  0) for r in state["results"])
            finished = state.get("finished", "")
            parts = []
            if inserted: parts.append(f"{inserted:,} inserted")
            if updated:  parts.append(f"{updated:,} updated")
            summary = " · ".join(parts) if parts else "no new data"
            sync_signal = int(pd.Timestamp.now().timestamp())
            return f"Done {finished} · {summary}", True, sync_signal

        return no_update, True, no_update

    # Tracks the last sync-done signal already reloaded for, so a completed sync
    # triggers exactly one _reload_all_data() call (not one per filter change).
    _reload_tracker = {"last_signal": 0}

    # ── Main render callback ────────────────────────────────────────────────────
    @app.callback(
        Output("eb-kpi-row",     "children"),
        Output("eb-filter-info", "children"),
        Output("eb-tab-content", "children"),
        Input("eb-dd-ds",      "value"),
        Input("eb-dd-prov",    "value"), Input("eb-dd-dist", "value"),
        Input("eb-dd-sec",     "value"), Input("eb-dd-hc",   "value"),
        Input("eb-date-rng",   "start_date"), Input("eb-date-rng", "end_date"),
        Input("eb-tabs",       "active_tab"),
        Input("eb-sync-done",  "data"),
    )
    def _update(ds, provs, dists, secs, hcs, start, end, tab, sync_signal):
        if not ds:
            eb.ensure_loaded()
            return [], "No eBuzima data loaded yet.", html.P(
                "No datasets available — click Sync Data below (or check eBuzima DB "
                "connectivity) to load data.", style={"color": eb.C["muted"]})

        # Reload in-memory data once per completed sync (not on every filter change).
        if sync_signal and sync_signal != _reload_tracker["last_signal"]:
            _reload_tracker["last_signal"] = sync_signal
            try:
                eb._reload_all_data()
            except Exception as exc:
                print(f"  [ebuzima] [warn] in-callback data reload failed: {exc}")

        filt = dict(provinces=provs, districts=dists, sectors=secs, hcs=hcs,
                    start=start, end=end)
        df = eb._apply(eb.active_df(ds), **filt)

        if "is_stunted" in df.columns:
            stunt = (df[df["age_years"].between(0, 5, inclusive="left")]
                     if "age_years" in df.columns else df)
        else:
            stunt = eb._apply(eb.STUNT, **filt)

        total = len(df)
        cards = [eb.card("Total Records", f"{total:,}", color=eb.C["primary"])]

        if "age_years" in df.columns and len(df):
            u5 = int((df["age_years"] < 5).sum())
            cards.append(eb.card("Under-5 Enrolled", f"{u5:,}",
                                  f"{u5/len(df)*100:.0f}% of dataset", eb.C["teal"]))

        if not stunt.empty and "is_stunted" in stunt.columns:
            n_st  = int(stunt["is_stunted"].sum())
            n_ss  = int(stunt["is_sev_stunted"].sum()) if "is_sev_stunted" in stunt.columns else 0
            tot_s = len(stunt)
            pct   = n_st / tot_s * 100 if tot_s else 0
            pct_s = n_ss / tot_s * 100 if tot_s else 0
            col   = eb.C["danger"] if pct >= 40 else eb.C["warning"] if pct >= 20 else eb.C["success"]
            cards.append(eb.card("Stunting Rate (U5)", f"{pct:.1f}%",
                                  f"{n_st:,}/{tot_s:,} assessed", col))
            cards.append(eb.card("Severe Stunting", f"{pct_s:.1f}%",
                                  f"HAZ ≤ −3  |  {n_ss:,} children", eb.C["danger"]))

        if not stunt.empty and "mal_cat" in stunt.columns:
            n_sam = int((stunt["mal_cat"] == "SAM").sum())
            pct_sam = n_sam / len(stunt) * 100 if len(stunt) else 0
            cards.append(eb.card("SAM", f"{pct_sam:.1f}%", f"{n_sam:,} children", eb.C["danger"]))

        if "discharged" in df.columns and len(df):
            n_d = int(df["discharged"].sum())
            cards.append(eb.card("Discharged", f"{n_d/len(df)*100:.1f}%",
                                  f"{n_d:,} patients", eb.C["success"]))

        dq = eb.dq_score(df)
        cards.append(eb.card("Data Quality", f"{dq:.0f}%", "field completeness",
                              eb.C["success"] if dq >= 80 else eb.C["warning"] if dq >= 60 else eb.C["danger"]))

        info = f"{total:,} records · {ds}"

        if   tab == "stunting":  content = eb.tab_stunting(stunt, df)
        elif tab == "discharge": content = eb.tab_discharge(df)
        elif tab == "cgm":       content = eb.tab_cgm(df)
        elif tab == "overview":  content = eb.tab_overview(df, filt)
        elif tab == "geo":       content = eb.tab_geo(df, stunt)
        elif tab == "demo":      content = eb.tab_demo(df)
        elif tab == "dq":        content = eb.tab_dq(df)
        else:                    content = html.P("Select a tab.")

        return dbc.Row([dbc.Col(c, md=True) for c in cards], className="g-2"), info, content


def eb_api_base_url() -> str:
    from ebuzima.api import BASE_URL
    return BASE_URL


def eb_api_key() -> str:
    from ebuzima.api import API_KEY
    return API_KEY


def eb_api_secret() -> str:
    from ebuzima.api import API_SECRET
    return API_SECRET
