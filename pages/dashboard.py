"""
pages/dashboard.py — Stunting analytics dashboard (5 tabs).

Tabs: Overview | Trends | Demographics | Immunization | Comparative
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from dash import Input, Output, State, dcc, html, no_update
import dash_bootstrap_components as dbc

from components.kpi import (
    C_DANGER, C_INFO, C_PRIMARY, C_SUCCESS, C_TEAL, C_WARNING, C_GREY,
    kpi_card, kpi_placeholder, metric_card,
)
from data import (
    district_options, filter_by_date, filter_by_user, filter_geo,
    get_all_df, get_df, hospital_options, nuniq, province_options,
    monthly_rate as _month_rate,   # shared with core/report_generator.py's trend chart
)

_H    = 340
_BG   = "rgba(0,0,0,0)"
_RED  = "#C0392B"
_FONT = dict(family="Inter,system-ui,sans-serif", size=12)
_MAR  = dict(l=40, r=20, t=36, b=40)


def _base(**kw) -> dict:
    return dict(paper_bgcolor=_BG, plot_bgcolor=_BG, font=_FONT, margin=_MAR, **kw)


def _empty(msg: str = "No data") -> go.Figure:
    fig = go.Figure()
    fig.update_layout(**_base(height=_H),
                      annotations=[dict(text=msg, xref="paper", yref="paper",
                                        x=0.5, y=0.5, showarrow=False,
                                        font=dict(size=14, color="#aaa"))])
    return fig


def _card(fig: go.Figure, h: int = _H) -> dbc.Card:
    return dbc.Card(
        dbc.CardBody(
            dcc.Graph(figure=fig, config={"displayModeBar": False},
                      style={"height": f"{h}px"}),
            className="p-2"),
        className="border-0 shadow-sm mb-3",
        style={"borderRadius": "8px"})


# ── Layout ─────────────────────────────────────────────────────────────────────

def layout(user: dict) -> html.Div:
    # Kick off the per-child WHO computation in the background so the KPI cards
    # and Overview charts can populate without blocking the page load.
    try:
        from data import warm_child_level
        warm_child_level()
    except Exception:
        pass

    df     = get_df()
    df_u   = filter_by_user(df, user) if df is not None else pd.DataFrame()
    role   = user.get("role", "")
    locked = role in ("hospital", "health_center")

    d_end   = datetime.today().strftime("%Y-%m-%d")
    # Default to showing all data since 2020 — date filter uses actual data range
    d_start = (
        df_u["immunization_date"].min().strftime("%Y-%m-%d")
        if df_u is not None and not df_u.empty
           and "immunization_date" in df_u.columns
           and df_u["immunization_date"].notna().any()
        else "2020-01-01"
    )

    return html.Div([
        # Header
        dbc.Row([
            dbc.Col(html.H5("Stunting Surveillance Dashboard",
                            className="fw-bold mb-0", style={"color": "#2C3E50"})),
            dbc.Col([
                html.Span(f"Data as of {datetime.today().strftime('%d %b %Y')}",
                          className="text-muted me-2", style={"fontSize": "0.78rem"}),
                dbc.Badge(
                    f"Source: {__import__('data').get_data_source().upper()}",
                    color="success" if __import__('data').get_data_source() == "postgresql"
                          else "secondary",
                    className="fw-semibold",
                    style={"fontSize": "0.7rem"},
                ),
            ], className="text-end d-flex align-items-center justify-content-end"),
        ], className="align-items-center mb-3"),

        # Filters
        dbc.Card(dbc.CardBody(
            dbc.Row([
                dbc.Col(dcc.Dropdown(id="db-prov",  placeholder="All Provinces",
                                     options=province_options(df_u), clearable=True,
                                     disabled=locked, style={"fontSize": "0.82rem"}),
                        xs=6, md=3),
                dbc.Col(dcc.Dropdown(id="db-dist",  placeholder="All Districts",
                                     options=district_options(df_u), clearable=True,
                                     disabled=locked, style={"fontSize": "0.82rem"}),
                        xs=6, md=3),
                dbc.Col(dcc.Dropdown(id="db-hosp",  placeholder="All Hospitals",
                                     options=hospital_options(df_u), clearable=True,
                                     disabled=locked, style={"fontSize": "0.82rem"}),
                        xs=6, md=3),
                dbc.Col(dcc.DatePickerRange(id="db-dates",
                                            start_date=d_start, end_date=d_end,
                                            display_format="DD/MM/YYYY",
                                            style={"fontSize": "0.82rem"}),
                        xs=6, md=3),
            ], className="g-2"),
            className="py-2 px-3"),
            className="mb-3 border-0 shadow-sm", style={"borderRadius": "8px"}),

        # KPI row (national headline — computed once, warmed in background)
        dbc.Row([kpi_placeholder() for _ in range(8)],
                id="db-kpi-row", className="g-2 mb-3"),
        dcc.Interval(id="db-kpi-poll", interval=2500, n_intervals=0),

        # Tabs
        dbc.Card(dbc.CardBody(
            dbc.Tabs([
                dbc.Tab(label="Overview",     tab_id="overview"),
                dbc.Tab(label="Trends",       tab_id="trends"),
                dbc.Tab(label="Demographics", tab_id="demo"),
                dbc.Tab(label="Immunization", tab_id="immuno"),
                dbc.Tab(label="Wasting",      tab_id="wasting"),
                dbc.Tab(label="Vaccination",  tab_id="vaccination"),
                dbc.Tab(label="Comparative",  tab_id="compare"),
            ], id="db-tabs", active_tab="overview", className="mb-3")),
            className="border-0 shadow-sm", style={"borderRadius": "8px"}),

        html.Div(id="db-tab-content", className="mt-0"),
    ])


# ── Callbacks ──────────────────────────────────────────────────────────────────

def _national_cards(res: dict) -> list:
    """The 7 headline KPI cards (national figures, deduplicated by child)."""
    def f(v):
        return f"{v:,}" if isinstance(v, int) else ("—" if v is None else str(v))

    return [
        metric_card("Total Vaccinated", f(res["total_vaccinated"]),
                    "unique children", C_INFO, "bi-people-fill"),
        metric_card("Total Stunted", f(res["current_total"]),
                    "current (latest visit)", C_PRIMARY, "bi-arrow-down-circle-fill"),
        metric_card("Ever Stunted", f(res["ever_stunted"]),
                    "stunted at any visit", C_WARNING, "bi-clock-history"),
        metric_card("Stunting Rate", f"{res['stunting_pct']}%",
                    "of assessed children", C_DANGER, "bi-graph-down-arrow"),
       
    ]


def register_callbacks(app) -> None:

    @app.callback(Output("db-dist", "options"),
                  Input("db-prov", "value"))
    def _dist_opts(province):
        from flask import session
        user = session.get("user")
        if not user:
            return []
        df = get_df()
        return district_options(filter_by_user(df, user) if df is not None else pd.DataFrame(), province)

    @app.callback(Output("db-hosp", "options"),
                  Input("db-dist", "value"),
                  State("db-prov", "value"))
    def _hosp_opts(district, province):
        from flask import session
        user = session.get("user")
        if not user:
            return []
        df = get_df()
        if df is None:
            return []
        df = filter_by_user(df, user)
        if province and "province" in df.columns:
            df = df[df["province"] == province]
        return hospital_options(df, district)

    @app.callback(
        Output("db-kpi-row", "children"),
        Output("db-kpi-poll", "disabled"),
        Input("db-kpi-poll", "n_intervals"),
        Input("db-prov",  "value"), Input("db-dist",  "value"),
        Input("db-hosp",  "value"), Input("db-dates", "start_date"),
        Input("db-dates", "end_date"),
    )
    def _kpis(_n, province, district, hospital, start, end):
        from flask import session
        user = session.get("user")
        if not user:
            return [kpi_placeholder() for _ in range(8)], True
        from data import (get_child_df, get_visits_df, warm_child_level,
                          child_build_status, scoped_child_df, summarize_children)
        child  = get_child_df()
        visits = get_visits_df()
        if child is None or visits is None:
            warm_child_level()                            # ensure it's running
            keep_polling = child_build_status() != "error"
            return [kpi_placeholder() for _ in range(8)], (not keep_polling)
        # Cheap for the default/wide range; only re-derives from raw visits
        # (correctly, per-period) when the date range is narrow — see
        # scoped_child_df's docstring.
        c = scoped_child_df(child, visits, user, province, district, hospital, start, end)
        return _national_cards(summarize_children(c)), True

    @app.callback(
        Output("db-tab-content", "children"),
        Input("db-tabs",  "active_tab"),
        Input("db-prov",  "value"), Input("db-dist",  "value"),
        Input("db-hosp",  "value"), Input("db-dates", "start_date"),
        Input("db-dates", "end_date"),
        Input("db-kpi-poll", "n_intervals"),   # re-render when the table finishes building
    )
    def _tab(tab, province, district, hospital, start, end, _poll):
        from flask import session
        user = session.get("user")
        if not user:
            return html.Div()
        df = get_df()
        if df is None or df.empty:
            return dbc.Alert("Data unavailable — check data/combined_df.csv",
                             color="warning", className="mt-3")
        df = filter_by_user(df, user)
        df = filter_geo(df, province, district, hospital)
        df = filter_by_date(df, start, end)

        # The Overview now uses the cached computed per-child table (+ monthly
        # trend), filtered identically. These reflect the WHO-computed stunting
        # so the charts match the KPI cards.
        if tab == "overview":
            from data import get_child_df, get_visits_df, get_monthly_df, get_schedule_df, scoped_child_df
            child_raw = get_child_df()
            visits    = get_visits_df()
            monthly   = get_monthly_df()
            schedule  = get_schedule_df()
            child = pd.DataFrame()
            if child_raw is not None and not child_raw.empty:
                child = scoped_child_df(child_raw, visits, user, province, district, hospital, start, end)
            if monthly is not None and not monthly.empty:
                monthly = filter_geo(monthly, province, district, hospital)
            if schedule is not None and not schedule.empty:
                schedule = filter_geo(schedule, province, district, hospital)
            return _overview(df, child, monthly, schedule, start, end)

        dispatch = {
            "trends":      _trends,
            "demo":        _demo,
            "immuno":      _immuno,
            "wasting":     _wasting,
            "vaccination": _vaccination,
            "compare":     _compare,
        }
        return dispatch.get(tab, lambda d: html.Div())(df)


# ── Tab builders ───────────────────────────────────────────────────────────────

TEI = "tracked_entity_instance"


# Friendly labels for vaccine columns across both the DB schema (bcg, opv…)
# and the CSV schema (vaccine_6_weeks, pcv1_dose…). Only present columns are used.
_VAX_LABELS = {
    "bcg": "BCG", "opv": "OPV", "hep_b_birth": "HepB (birth)",
    "dpt_hepb_hib": "DPT-HepB-Hib", "pneumococcal": "PCV",
    "rotavirus": "Rotavirus", "measles_rubella": "MR", "hpv": "HPV",
    "vaccine_6_weeks": "6-week visit", "vaccine_10_weeks": "10-week visit",
    "vaccine_14_weeks": "14-week visit",
    "pcv1_dose": "PCV-1", "pcv2_dose": "PCV-2", "pcv3_dose": "PCV-3",
    "rotavirus1_dose": "Rota-1", "rotavirus2_dose": "Rota-2",
}


def _tei_col(df: pd.DataFrame) -> str | None:
    for c in ("entity_id", TEI):
        if c in df.columns:
            return c
    return None


def _severity_masks(df: pd.DataFrame):
    """Return (severe_mask, moderate_mask) for a stunted DataFrame, handling both
    the CSV 'severe_stunting' column and the DB 'stunting_status' wording."""
    col = "severe_stunting" if "severe_stunting" in df.columns else "stunting_status"
    if col not in df.columns:
        empty = pd.Series(False, index=df.index)
        return empty, empty
    s = df[col].astype(str).str.lower()
    severe   = s.str.contains("sever", na=False)
    moderate = s.str.contains("moderat", na=False)
    # If the column is just "Stunted/Normal" (no severity), treat all as moderate
    if not severe.any() and not moderate.any():
        moderate = df["stunting_status"].astype(str).str.lower().str.contains("stunt", na=False)
    return severe, moderate


def _coverage_table(df: pd.DataFrame) -> pd.DataFrame:
    """Vaccination coverage % per antigen among UNIQUE stunted children
    (latest visit per child). Works on either data schema."""
    tei = _tei_col(df)
    cols = [c for c in _VAX_LABELS if c in df.columns]
    if df is None or df.empty or not cols or tei is None:
        return pd.DataFrame()
    latest = (df.sort_values("immunization_date", ascending=False)
                .drop_duplicates(subset=[tei])
              if "immunization_date" in df.columns else df)
    total = int(latest[tei].nunique()) if tei in latest.columns else len(latest)

    def _given(series: pd.Series) -> pd.Series:
        num = pd.to_numeric(series, errors="coerce")
        num_given = (num >= 1).fillna(False)
        txt = series.astype(str).str.strip().str.lower()
        txt_given = txt.isin(["yes", "true", "given", "done", "administered"])
        return num_given | txt_given

    rows = []
    for c in cols:
        g = int(_given(latest[c]).sum())
        rows.append({"Vaccine": _VAX_LABELS[c], "Coverage": round(g / max(total, 1) * 100, 1),
                     "Given": g, "Total": total})
    return pd.DataFrame(rows)


def _sched_sort_key(value) -> tuple:
    """Robustly order EPI visits regardless of casing/spacing/spelling:
    At Birth → weeks (by number) → months (by number) → years.
    e.g. 'at birth', '6 Weeks', '6 weeks', '10 WEEKS', '9 Months', '15 months'."""
    import re
    n = str(value).strip().lower()
    if "birth" in n:
        return (0, 0)
    m = re.search(r"(\d+)", n)
    num = int(m.group(1)) if m else 999
    if "week" in n:
        return (1, num)
    if "month" in n:
        return (2, num)
    if "year" in n:
        return (3, num)
    return (9, num)


def _overview(df: pd.DataFrame, child=None, monthly=None, schedule=None,
              start=None, end=None) -> html.Div:
    have_child = child is not None and not child.empty
    have_month = monthly is not None and not monthly.empty
    have_sched = schedule is not None and not schedule.empty
    building = "Waiting for the computation"

    # ── 1. Stunting RATE over time (WHO computed, future/thin months removed) ──
    fig_rate = _empty(building if not have_month else "No date data")
    if have_month:
        m = _month_rate(monthly, start, end, "rate")
        if not m.empty:
            fig_rate = px.area(m, x="month", y="y", text="y",
                               title="Stunting Rate Over Time(%)",
                               color_discrete_sequence=[_RED])
            fig_rate.update_traces(
                mode="lines+markers+text",
                texttemplate="%{text:.1f}%", textposition="top center",
                textfont=dict(size=10, color="#7B241C"), cliponaxis=False,
                hovertemplate="%{x|%b %Y}<br>%{y:.1f}%<extra></extra>")
            fig_rate.update_layout(**_base(height=_H, yaxis_title="Stunting %",
                                           xaxis_title=""))
            # headroom + margins so the % labels (incl. the last month) aren't clipped
            fig_rate.update_layout(margin=dict(l=48, r=40, t=48, b=40))
            fig_rate.update_yaxes(range=[0, max(float(m["y"].max()) * 1.28, 5)])
            fig_rate.update_xaxes(automargin=True)
            if len(m) == 1:
                # A single point on a date axis auto-ranges to a sub-second
                # window (Plotly picks a default span around one timestamp) —
                # pin a sensible +/-15 day window around it instead.
                mid = m["month"].iloc[0]
                fig_rate.update_xaxes(range=[mid - pd.Timedelta(days=15),
                                             mid + pd.Timedelta(days=15)])

    # ── 2. Geographic hotspots by PREVALENCE rate (computed) ──────────────────
    fig_hot = _empty(building if not have_child else "No district data")
    if have_child and "district" in child.columns:
        g = (child.dropna(subset=["district"])
                  .assign(_s=child["is_stunted"].eq(True))
                  .groupby("district")
                  .agg(n=("_s", "size"), stunted=("_s", "sum")).reset_index())
        # A "min 50 per district" noise floor makes sense for the default
        # all-time view, but can exceed the entire scoped population for a
        # narrow date range — scale it down there instead of hiding the chart.
        narrow_range = False
        if start and end:
            try:
                narrow_range = (pd.Timestamp(end) - pd.Timestamp(start)).days < 180
            except Exception:
                narrow_range = False
        g = g[g["n"] >= (3 if narrow_range else 50)]
        g["rate"] = (g["stunted"] / g["n"] * 100).round(1)
        g = g.sort_values("rate").tail(15)
        if not g.empty:
            fig_hot = px.bar(g, x="rate", y="district", orientation="h", text="rate",
                             color="rate", color_continuous_scale=["#FCF3CF", _RED],
                             title="Geographic Hotspots — Stunting Prevalence % (top 15)",
                             hover_data={"stunted": True, "n": True})
            fig_hot.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
            fig_hot.update_layout(**_base(height=max(_H, len(g) * 30 + 80),
                                          coloraxis_showscale=False,
                                          xaxis_title="Prevalence %", yaxis_title=""))

    # ── 3. Severe vs Moderate split (computed) + severe-share trend ───────────
    fig_sev = _empty(building if not have_child else "No severity data")
    if have_child:
        sev = int(child["is_severe"].eq(True).sum())
        tot = int(child["is_stunted"].eq(True).sum())
        mod = max(tot - sev, 0)
        if sev + mod > 0:
            sv = pd.DataFrame({"Severity": ["Severely Stunted", "Moderately Stunted"],
                               "Children": [sev, mod]})
            fig_sev = px.pie(sv, names="Severity", values="Children", hole=0.45,
                             title="Severe vs Moderate Stunting",
                             color="Severity",
                             color_discrete_map={"Severely Stunted": "#922B21",
                                                 "Moderately Stunted": "#E59866"})
            fig_sev.update_layout(**_base(height=_H))

    fig_sevtrend = _empty(building if not have_month else "No date data")
    if have_month:
        ms = _month_rate(monthly, start, end, "severe_share")
        if not ms.empty and ms["y"].sum() > 0:
            fig_sevtrend = px.line(ms, x="month", y="y", markers=True, text="y",
                                   title="Severe Share of Stunted Children Over Time (%)",
                                   color_discrete_sequence=["#922B21"])
            fig_sevtrend.update_traces(
                texttemplate="%{text:.1f}%", textposition="top center",
                textfont=dict(size=10, color="#922B21"), cliponaxis=False,
                hovertemplate="%{x|%b %Y}<br>%{y:.1f}%<extra></extra>")
            fig_sevtrend.update_layout(**_base(height=_H, yaxis_title="Severe %", xaxis_title=""))
            fig_sevtrend.update_layout(margin=dict(l=48, r=40, t=48, b=40))
            fig_sevtrend.update_yaxes(range=[0, max(float(ms["y"].max()) * 1.28, 5)])
            if len(ms) == 1:
                mid = ms["month"].iloc[0]
                fig_sevtrend.update_xaxes(range=[mid - pd.Timedelta(days=15),
                                                 mid + pd.Timedelta(days=15)])

    # ── 3b. Stunting by immunization schedule (EPI visit) ─────────────────────
    fig_sched = _empty(building if not have_sched else "No schedule data")
    if have_sched:
        s = (schedule.groupby("immunization_schedule")
                     .agg(measured=("measured", "sum"), stunted=("stunted", "sum"))
                     .reset_index())
        s = s[s["measured"] >= 30]
        # Keep only the EPI window: At Birth → weeks → months up to 15.
        # Drop anything in years (e.g. "12 Years") or months beyond 15.
        def _in_epi(v):
            k = _sched_sort_key(v)
            return k[0] <= 1 or (k[0] == 2 and k[1] <= 15)
        s = s[s["immunization_schedule"].apply(_in_epi)]
        if not s.empty:
            s["rate"] = (s["stunted"] / s["measured"] * 100).round(1)
            # Order by parsed EPI position (birth → weeks → months → years),
            # robust to how eTracker capitalises/spaces the schedule label.
            cat_order = sorted(dict.fromkeys(s["immunization_schedule"]),
                               key=_sched_sort_key)
            s["immunization_schedule"] = pd.Categorical(
                s["immunization_schedule"], categories=cat_order, ordered=True)
            s = s.sort_values("immunization_schedule")
            fig_sched = px.line(s, x="immunization_schedule", y="rate", markers=True, text="rate",
                                title="Stunting Rate by Immunization Schedule (%)",
                                color_discrete_sequence=[_RED],
                                category_orders={"immunization_schedule": cat_order})
            fig_sched.update_traces(
                texttemplate="%{text:.1f}%", textposition="top center",
                textfont=dict(size=11, color="#7B241C"), cliponaxis=False,
                hovertemplate="%{x}<br>%{y:.1f}%<extra></extra>")
            fig_sched.update_layout(**_base(height=_H, yaxis_title="Stunting %",
                                            xaxis_title="EPI visit"))
            fig_sched.update_layout(margin=dict(l=48, r=30, t=48, b=44))
            fig_sched.update_yaxes(range=[0, max(float(s["rate"].max()) * 1.28, 5)])
            fig_sched.update_xaxes(categoryorder="array", categoryarray=cat_order)

    # ── 4. Vaccination coverage gap (among stunted children, column df) ───────
    fig_cov = _empty("Vaccine columns not in current data source")
    cov = _coverage_table(df)
    if not cov.empty:
        cov = cov.sort_values("Coverage", ascending=True)
        n_u = cov["Total"].iloc[0]
        fig_cov = px.bar(cov, x="Coverage", y="Vaccine", orientation="h", text="Coverage",
                         color="Coverage", color_continuous_scale=[_RED, "#F9E79F", "#1E8449"],
                         range_color=[0, 100],
                         title=f"Vaccination Coverage Gap — {n_u:,} unique stunted children",
                         hover_data={"Given": True, "Total": True})
        fig_cov.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
        fig_cov.update_layout(**_base(height=max(_H, len(cov) * 34 + 80),
                                      coloraxis_showscale=False, xaxis_title="Coverage %",
                                      xaxis_range=[0, 110]))

    note = html.Div()
    if not have_child:
        note = dbc.Alert(
            [html.I(className="bi bi-hourglass-split me-2"),
             "WHO stunting is being computed in the background. These charts will "
             "fill in shortly — switch tabs and back, or refresh in ~1 minute."],
            color="light", className="py-2 mb-3 border", style={"fontSize": "0.8rem"})

    return html.Div([
        note,
        dbc.Row([dbc.Col(_card(fig_rate),  md=6),
                 dbc.Col(_card(fig_hot),   md=6)], className="g-3 mb-0"),
        dbc.Row([dbc.Col(_card(fig_sched), md=7),
                 dbc.Col(_card(fig_sev),   md=5)], className="g-3 mb-0"),
        _card(fig_sevtrend),
        _card(fig_cov),
    ])


def _trends(df: pd.DataFrame) -> html.Div:
    fig_m = fig_q = fig_s = _empty()

    if "immunization_date" in df.columns and df["immunization_date"].notna().any():
        dd = df[df["immunization_date"].notna()].copy()
        dd["ym"] = dd["immunization_date"].dt.to_period("M").astype(str)
        m = dd.groupby("ym")[TEI].nunique().reset_index()
        m.columns = ["Period", "Children"]
        fig_m = px.line(m, x="Period", y="Children", markers=True,
                        title="Monthly Cases", color_discrete_sequence=[_RED])
        fig_m.update_layout(**_base(height=_H))
        fig_m.update_yaxes(tickformat=",d")
        fig_m.update_traces(hovertemplate="%{x}<br>%{y:,} children<extra></extra>")

        dd["q"] = dd["immunization_date"].dt.to_period("Q").astype(str)
        q = dd.groupby("q")[TEI].nunique().reset_index()
        q.columns = ["Quarter", "Children"]
        fig_q = px.bar(q, x="Quarter", y="Children", text="Children",
                       title="Quarterly Cases", color_discrete_sequence=["#2980B9"])
        fig_q.update_layout(**_base(height=_H))
        fig_q.update_yaxes(tickformat=",d")
        fig_q.update_traces(texttemplate="%{y:,}", textposition="outside",
                            hovertemplate="%{x}<br>%{y:,} children<extra></extra>")

        MN = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
        dd["mo"] = dd["immunization_date"].dt.month
        s = dd.groupby("mo")[TEI].nunique().reset_index()
        s["Month"] = s["mo"].apply(lambda x: MN[x - 1])
        fig_s = px.bar(s, x="Month", y=TEI, title="Seasonal Pattern",
                       color=TEI, color_continuous_scale=["#FADBD8", _RED],
                       labels={TEI: "Children"})
        fig_s.update_layout(**_base(height=_H, coloraxis_showscale=False))
        fig_s.update_yaxes(tickformat=",d")
        fig_s.update_traces(hovertemplate="%{x}<br>%{y:,} children<extra></extra>")

    return html.Div([
        _card(fig_m),
        dbc.Row([dbc.Col(_card(fig_q), md=6), dbc.Col(_card(fig_s), md=6)], className="g-3"),
    ])


def _demo(df: pd.DataFrame) -> html.Div:
    fig_g = fig_a = fig_b = _empty()

    if "gender" in df.columns:
        g = df[df["gender"].notna()].groupby("gender")[TEI].nunique().reset_index()
        g.columns = ["Gender", "Children"]
        if not g.empty:
            fig_g = px.pie(g, names="Gender", values="Children",
                           title="Gender Split", hole=0.45,
                           color_discrete_sequence=["#2980B9", "#E74C3C", "#27AE60"])
            fig_g.update_layout(**_base(height=_H))

    if "age_in_months" in df.columns:
        ages = pd.to_numeric(df["age_in_months"], errors="coerce").dropna()
        if len(ages):
            df2 = df.copy()
            df2["age_in_months"] = pd.to_numeric(df2["age_in_months"], errors="coerce")
            bins   = [0, 6, 12, 18, 24, 36, 48, 60, 999]
            labels = ["0–6", "6–12", "12–18", "18–24", "24–36", "36–48", "48–60", "60+"]
            df2["grp"] = pd.cut(df2["age_in_months"], bins=bins, labels=labels, right=False)
            ac = df2.groupby("grp", observed=True)[TEI].nunique().reset_index()
            ac.columns = ["Age (months)", "Children"]
            fig_a = px.bar(ac, x="Age (months)", y="Children",
                           title="Age Distribution",
                           color="Children", color_continuous_scale=["#FADBD8", _RED])
            fig_a.update_layout(**_base(height=_H, coloraxis_showscale=False))

            if "gender" in df.columns:
                db = df2[df2["gender"].notna() & df2["age_in_months"].notna()]
                fig_b = px.box(db, x="gender", y="age_in_months", color="gender",
                               title="Age by Gender",
                               color_discrete_sequence=["#2980B9", "#E74C3C"],
                               labels={"age_in_months": "Age (months)", "gender": ""})
                fig_b.update_layout(**_base(height=_H, showlegend=False))

    return dbc.Row([
        dbc.Col(_card(fig_g), md=4),
        dbc.Col(_card(fig_a), md=4),
        dbc.Col(_card(fig_b), md=4),
    ], className="g-3")


def _immuno(df: pd.DataFrame) -> html.Div:
    fig_s = fig_sun = _empty()

    if "immunization_schedule" in df.columns:
        s = (df[df["immunization_schedule"].notna()]
             .groupby("immunization_schedule")[TEI].nunique()
             .sort_values(ascending=True).reset_index())
        s.columns = ["Schedule", "Children"]
        if not s.empty:
            fig_s = px.bar(s, x="Children", y="Schedule", orientation="h",
                           title="Stunted Children by Immunization Schedule",
                           color="Children", color_continuous_scale=["#FADBD8", _RED])
            fig_s.update_layout(**_base(height=max(_H, len(s) * 36),
                                        coloraxis_showscale=False))

        if "district" in df.columns:
            top_d = df["district"].dropna().value_counts().head(8).index.tolist()
            sun = (df[df["district"].isin(top_d) & df["immunization_schedule"].notna()]
                   .groupby(["district", "immunization_schedule"])[TEI].nunique()
                   .reset_index())
            sun.columns = ["district", "schedule", "count"]
            if not sun.empty:
                fig_sun = px.sunburst(sun, path=["district", "schedule"], values="count",
                                      title="District → Schedule Breakdown (top 8)",
                                      color="count",
                                      color_continuous_scale=["#FADBD8", _RED])
                fig_sun.update_layout(**_base(height=480, coloraxis_showscale=False))

    return html.Div([_card(fig_s), _card(fig_sun, h=480)])


def _wasting(df: pd.DataFrame) -> html.Div:
    """Wasting status breakdown — stunted children who are also wasted."""
    fig_w = fig_wdist = fig_comb = _empty()

    if "wasting_status" in df.columns:
        w = (df[df["wasting_status"].notna()]
             .groupby("wasting_status")[TEI].nunique().reset_index())
        w.columns = ["Status", "Children"]
        if not w.empty:
            fig_w = px.pie(w, names="Status", values="Children",
                           title="Wasting Status Distribution", hole=0.45,
                           color_discrete_sequence=["#E74C3C","#F39C12","#27AE60","#3498DB"])
            fig_w.update_layout(**_base(height=_H))

        if "district" in df.columns:
            wd = (df[df["wasting_status"].notna() & df["district"].notna()]
                  .groupby(["district", "wasting_status"])[TEI].nunique()
                  .reset_index())
            wd.columns = ["District", "Status", "Children"]
            top10 = df["district"].dropna().value_counts().head(10).index
            wd = wd[wd["District"].isin(top10)]
            if not wd.empty:
                fig_wdist = px.bar(wd, x="District", y="Children", color="Status",
                                   title="Wasting Status by District (top 10)",
                                   barmode="stack",
                                   color_discrete_sequence=["#E74C3C","#F39C12","#27AE60"])
                fig_wdist.update_layout(**_base(height=_H, legend_title=""))

        # Combined stunting + wasting
        if "stunting_status" in df.columns:
            def _combo(row):
                s = str(row.get("stunting_status", "") or "").lower()
                w = str(row.get("wasting_status",  "") or "").lower()
                if "stunt" in s and "wast" in w:
                    return "Stunted + Wasted"
                if "stunt" in s:
                    return "Stunted only"
                if "wast" in w:
                    return "Wasted only"
                return "Neither"
            df2 = df.copy()
            df2["combo"] = df2.apply(_combo, axis=1)
            cb = df2.groupby("combo")[TEI].nunique().reset_index()
            cb.columns = ["Condition", "Children"]
            if not cb.empty:
                fig_comb = px.bar(cb.sort_values("Children", ascending=True),
                                  x="Children", y="Condition", orientation="h",
                                  title="Stunting & Wasting Overlap",
                                  color="Children",
                                  color_continuous_scale=["#FADBD8", _RED])
                fig_comb.update_layout(**_base(height=260, coloraxis_showscale=False))

    return html.Div([
        dbc.Row([dbc.Col(_card(fig_w),     md=4),
                 dbc.Col(_card(fig_wdist), md=8)], className="g-3 mb-0"),
        _card(fig_comb, h=260),
    ])


def _vaccination(df: pd.DataFrame) -> html.Div:
    """
    Vaccination coverage using deduplicated children (by entity_id or TEI).
    Flags children with missed vaccinations based on Rwanda EPI schedule + age.
    """
    from core.risk_classifier import vaccination_coverage_summary, flag_missed_vaccinations

    fig_cov = fig_sched = fig_missed = _empty()

    # ── Coverage chart ────────────────────────────────────────────────────────
    # Use the risk_classifier's deduplicated summary (entity_id deduplication)
    cov_df = vaccination_coverage_summary(df)
    if not cov_df.empty:
        cov_df = cov_df.sort_values("coverage", ascending=True)
        n_total = cov_df["total"].iloc[0] if len(cov_df) else 0
        fig_cov = px.bar(
            cov_df, x="coverage", y="vaccine", orientation="h",
            title=f"EPI Vaccination Coverage — {n_total:,} unique children (stunted)",
            color="coverage",
            color_continuous_scale=["#FADBD8", "#C0392B"],
            text="coverage",
            hover_data={"given": True, "total": True},
        )
        fig_cov.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
        fig_cov.update_layout(**_base(height=max(_H, len(cov_df) * 52 + 80),
                                      coloraxis_showscale=False,
                                      xaxis_title="Coverage %", yaxis_title=""))

    # ── Missed-vaccine flagging ────────────────────────────────────────────────
    # Deduplicate to latest visit per child before flagging
    tei_col = ("entity_id" if "entity_id" in df.columns
               else TEI if TEI in df.columns else None)
    latest_df = (
        df.sort_values("immunization_date", ascending=False)
          .drop_duplicates(subset=[tei_col])
        if tei_col and "immunization_date" in df.columns
        else df
    )

    flagged = flag_missed_vaccinations(latest_df)
    n_any_missed = int((flagged["missed_count"] > 0).sum()) if "missed_count" in flagged.columns else 0

    if n_any_missed > 0 and "missed_vaccines" in flagged.columns:
        # Flatten missed vaccine names → count per vaccine
        from collections import Counter
        all_missed = [v for lst in flagged["missed_vaccines"] for v in lst]
        missed_counts = Counter(all_missed)
        m_df = pd.DataFrame(missed_counts.items(), columns=["Vaccine", "Missed Count"])
        m_df = m_df.sort_values("Missed Count", ascending=True)
        fig_missed = px.bar(
            m_df, x="Missed Count", y="Vaccine", orientation="h",
            title=f"Missed Vaccines — {n_any_missed:,} children with ≥1 missed dose",
            color="Missed Count",
            color_continuous_scale=["#FEF9E7", "#E67E22"],
        )
        fig_missed.update_layout(**_base(height=max(_H, len(m_df) * 52 + 80),
                                         coloraxis_showscale=False))

    # ── Schedule distribution ─────────────────────────────────────────────────
    if "immunization_schedule" in df.columns and tei_col in df.columns:
        s = (df[df["immunization_schedule"].notna()]
             .groupby("immunization_schedule")[tei_col].nunique()
             .sort_values(ascending=False).head(15).reset_index())
        s.columns = ["Schedule", "Children"]
        if not s.empty:
            fig_sched = px.bar(s, x="Children", y="Schedule", orientation="h",
                               title="Stunted Children by EPI Visit Schedule",
                               color="Children",
                               color_continuous_scale=["#D6EAF8", "#2980B9"])
            fig_sched.update_layout(**_base(height=max(_H, len(s) * 36 + 60),
                                            coloraxis_showscale=False))

    # ── Missed-vaccine alert badge ────────────────────────────────────────────
    total_u = nuniq(df)
    missed_alert = html.Div()
    if n_any_missed > 0:
        missed_pct = round(n_any_missed / max(total_u, 1) * 100, 1)
        missed_alert = dbc.Alert(
            [html.I(className="bi bi-exclamation-triangle-fill me-2"),
             f"{n_any_missed:,} of {total_u:,} children ({missed_pct}%) have at least "
             f"one missed vaccine based on Rwanda EPI schedule and age."],
            color="warning", className="mb-3 py-2", style={"fontSize": "0.83rem"},
        )

    return html.Div([
        missed_alert,
        dbc.Row([dbc.Col(_card(fig_cov),    md=7),
                 dbc.Col(_card(fig_missed), md=5)], className="g-3 mb-3"),
        dbc.Row([dbc.Col(_card(fig_sched), md=12)], className="g-3"),
    ])


def _compare(df: pd.DataFrame) -> html.Div:
    fig_bar = fig_gen = fig_heat = _empty()

    if "district" in df.columns:
        top_d = df["district"].dropna().value_counts().head(10).index.tolist()
        dc = df[df["district"].isin(top_d)]

        cnt = dc.groupby("district")[TEI].nunique().sort_values(ascending=False).reset_index()
        cnt.columns = ["District", "Children"]
        if not cnt.empty:
            fig_bar = px.bar(cnt, x="District", y="Children",
                             title="Cases by District (top 10)",
                             color="Children", color_continuous_scale=["#FADBD8", _RED])
            fig_bar.update_layout(**_base(height=_H, coloraxis_showscale=False))

        if "gender" in dc.columns:
            gd = (dc[dc["gender"].notna()]
                  .groupby(["district", "gender"])[TEI].nunique().reset_index())
            gd.columns = ["District", "Gender", "Children"]
            if not gd.empty:
                fig_gen = px.bar(gd, x="District", y="Children", color="Gender",
                                 barmode="group", title="Gender Split by District",
                                 color_discrete_sequence=["#2980B9", "#E74C3C"])
                fig_gen.update_layout(**_base(height=_H))

        if "immunization_schedule" in dc.columns:
            heat = (dc[dc["immunization_schedule"].notna()]
                    .groupby(["district", "immunization_schedule"])[TEI].nunique()
                    .unstack(fill_value=0))
            if not heat.empty:
                fig_heat = go.Figure(go.Heatmap(
                    z=heat.values, x=heat.columns.tolist(), y=heat.index.tolist(),
                    colorscale=[[0, "#FADBD8"], [1, _RED]],
                    hovertemplate="District: %{y}<br>Schedule: %{x}<br>Children: %{z:,}<extra></extra>",
                ))
                fig_heat.update_layout(**_base(height=max(300, len(heat) * 30 + 60)),
                                       title="Heatmap: District × Schedule",
                                       xaxis_title="", yaxis_title="")

    return html.Div([
        dbc.Row([dbc.Col(_card(fig_bar), md=6), dbc.Col(_card(fig_gen), md=6)],
                className="g-3 mb-0"),
        _card(fig_heat, h=max(300, 300)),
    ])
