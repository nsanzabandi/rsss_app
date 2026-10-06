"""
pages/risk_dashboard.py — At-risk (growth faltering) dashboard.

Definition (same everywhere on the dashboards): a child is at risk if their
LATEST weighed visit in the selected period shows HIGH or MEDIUM growth risk
(core.risk_classifier.classify_visits) — weight loss, severe/moderate wasting,
low MUAC, or weight/height gain far below the WHO minimum for their age.
All measured children are covered (early warning), with an optional
"stunted only" view. Data comes from the precomputed per-visit risk table
(data.get_risk_df), so every filter change is instant.

Opened from the main dashboard's "At-Risk Children" card, which passes its
filters in the URL (?prov=&dist=&hosp=&from=&to=).
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import parse_qs

import dash_bootstrap_components as dbc
import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, dcc, html, no_update

from components.kpi import metric_card

_HIGH, _MED = "#B03A2E", "#E0A100"
_FONT = dict(family="Inter,system-ui,sans-serif", size=12, color="#1F2D3D")
_AGE_BINS   = [-0.1, 5.99, 11.99, 23.99, 59.99, 1000]
_AGE_LABELS = ["0–5 mo", "6–11 mo", "12–23 mo", "24–59 mo", "5 yrs +"]


def _fig(height: int = 300) -> go.Figure:
    f = go.Figure()
    f.update_layout(height=height, margin=dict(l=10, r=16, t=10, b=30), font=_FONT,
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    legend=dict(orientation="h", y=1.12, x=0))
    f.update_xaxes(gridcolor="#EEF1F5", zeroline=False)
    f.update_yaxes(gridcolor="#EEF1F5", zeroline=False)
    return f


def _empty(msg: str = "No data for this selection") -> go.Figure:
    f = _fig(260)
    f.add_annotation(text=msg, showarrow=False, font=dict(color="#8A99A8", size=13))
    f.update_xaxes(visible=False); f.update_yaxes(visible=False)
    return f


def _chart_card(title: str, graph_id: str, hint: str | None = None) -> dbc.Card:
    return dbc.Card(dbc.CardBody([
        html.Div(title, className="nhic-card-title"),
        html.Div(hint, className="nhic-hint") if hint else None,
        dcc.Graph(id=graph_id, config={"displayModeBar": False}, figure=_empty("Loading…")),
    ]), className="nhic-card h-100")


# ── Layout ─────────────────────────────────────────────────────────────────────

def layout(user: dict, search: str = "") -> html.Div:
    import data
    from pages.dashboard import _period_options, period_range
    q = {k: v[0] for k, v in parse_qs((search or "").lstrip("?")).items()}

    risk = data.get_risk_df()
    first = (risk["immunization_date"].min().strftime("%Y-%m-%d")
             if risk is not None and not risk.empty else "2020-01-01")
    today = datetime.today().strftime("%Y-%m-%d")
    start, end = q.get("from", first), q.get("to", today)

    child = data.get_child_df()
    child_u = data.filter_by_user(child, user) if child is not None else pd.DataFrame()
    locked = user.get("role") in ("hospital", "health_center")
    dd = {"fontSize": "0.82rem"}
    period_opts = _period_options(first)
    period_val = next((o["value"] for o in period_opts
                       if o["value"] != "all" and period_range(o["value"]) == (start, end)),
                      "all" if (start, end) == (first, today) else None)

    return html.Div([
        dcc.Download(id="rd-download"),
        dbc.Card(dbc.CardBody(dbc.Row([
            dbc.Col(dcc.Dropdown(id="rd-prov", placeholder="All Provinces", value=q.get("prov"),
                                 options=data.province_options(child_u), disabled=locked, style=dd),
                    xs=6, md=4, xl=2),
            dbc.Col(dcc.Dropdown(id="rd-dist", placeholder="All Districts", value=q.get("dist"),
                                 options=data.district_options(child_u), disabled=locked, style=dd),
                    xs=6, md=4, xl=2),
            dbc.Col(dcc.Dropdown(id="rd-hosp", placeholder="All Hospitals", value=q.get("hosp"),
                                 options=data.hospital_options(child_u), disabled=locked, style=dd),
                    xs=6, md=4, xl=2),
            dbc.Col(dcc.Dropdown(id="rd-period", options=period_opts, value=period_val,
                                 clearable=False, searchable=False, placeholder="Custom dates",
                                 style=dd), xs=6, md=4, xl=2),
            dbc.Col(dcc.DatePickerRange(id="rd-dates", start_date=start, end_date=end,
                                        display_format="DD/MM/YYYY"), xs=12, md=True),
            dbc.Col(dbc.Button([html.I(className="bi bi-download me-1"), "Download list"],
                               id="rd-dl-btn", size="sm", color="light",
                               className="nhic-dl-btn") if not user.get("readonly") else None,
                    width="auto", className="ms-auto"),
        ], className="g-2 align-items-center"), className="py-2 px-3"),
            className="mb-2 border-0 shadow-sm", style={"borderRadius": "8px"}),

        html.Div([
            dbc.RadioItems(id="rd-cohort", inline=True, value="all", className="nhic-hint",
                           options=[{"label": " All measured children", "value": "all"},
                                    {"label": " Stunted children only", "value": "stunted"}]),
            html.Span("At risk = HIGH or MEDIUM growth risk at the child's latest weighed visit "
                      "in the period.", className="nhic-hint ms-md-3"),
        ], className="d-flex flex-wrap align-items-center mb-2 px-1"),
        html.Div(id="rd-dl-msg"),

        dbc.Row(id="rd-kpis", className="g-2 mb-2"),
        dbc.Row([
            dbc.Col(_chart_card("Why children are at risk", "rd-reasons",
                                "A child can have more than one reason."), lg=5),
            dbc.Col(_chart_card("At-risk trend", "rd-trend",
                                "% of children weighed each month (all children)."), lg=7),
        ], className="g-3 mb-3"),
        dbc.Row([
            dbc.Col(_chart_card("Hotspots", "rd-hotspots",
                                "Where the share of at-risk children is highest."), lg=7),
            dbc.Col(_chart_card("By age", "rd-age"), lg=5),
        ], className="g-3 mb-3"),
        dbc.Card(dbc.CardBody([
            html.Div([html.I(className="bi bi-list-check"), "Children to follow up first"],
                     className="nhic-card-title"),
            html.Div("HIGH risk first, then the largest weight loss. Showing up to 100 — "
                     "use Download list for everyone.", className="nhic-hint mb-2"),
            dcc.Loading(html.Div(id="rd-table"), type="dot", color="#0078C0"),
        ]), className="nhic-card mb-3"),
    ])


# ── Builders ───────────────────────────────────────────────────────────────────

def _next_level(user, province, district, hospital) -> tuple[str, str] | None:
    role = user.get("role")
    if role == "health_center":
        return None
    if hospital or role == "hospital":
        return "health_facility", "Health facility"
    if district or role == "district":
        return "district_hospital", "Hospital"
    return "district", "District"


def _reasons_fig(sm: dict) -> go.Figure:
    items = sorted(((v, k) for k, v in sm["reasons"].items() if v), reverse=False)
    if not items:
        return _empty("No at-risk children")
    f = _fig(max(220, 46 * len(items)))
    f.add_bar(y=[k for _, k in items], x=[v for v, _ in items], orientation="h",
              marker_color="#0078C0", text=[f"{v:,}" for v, _ in items], textposition="outside",
              hovertemplate="%{y}: %{x:,} children<extra></extra>")
    f.update_traces(cliponaxis=False)
    f.update_xaxes(visible=False, range=[0, max(v for v, _ in items) * 1.18])
    f.update_layout(margin=dict(l=10, r=20, t=10, b=10))
    return f


def _trend_fig(user, province, district, hospital, start, end) -> go.Figure:
    import data
    m = data.get_risk_monthly_df()
    if m is None or m.empty:
        return _empty()
    m = data.filter_geo(data.filter_by_user(m, user), province, district, hospital)
    # Always show at least 12 months up to the period end, so a single month or
    # quarter is seen in context; the selected period is shaded.
    p_end = pd.Timestamp(end) if end else pd.Timestamp.today()
    p_start = pd.Timestamp(start).to_period("M").to_timestamp() if start else None
    ctx_start = (p_end.to_period("M") - 11).to_timestamp()
    lo = min(ctx_start, p_start) if p_start is not None else ctx_start
    m = m[(m["month"] >= lo) & (m["month"] <= p_end)]
    g = m.groupby("month")[["measured", "high", "medium"]].sum().reset_index()
    if g.empty:
        return _empty()
    g = g[g["measured"] >= max(30, 0.3 * g["measured"].quantile(0.75))]   # drop thin months
    if g.empty:
        return _empty("Too few children weighed in this period")
    hi = (g["high"] / g["measured"] * 100).round(1)
    me = (g["medium"] / g["measured"] * 100).round(1)
    f = _fig()
    f.add_scatter(x=g["month"], y=hi, name="HIGH", stackgroup="r", line=dict(color=_HIGH, width=1),
                  hovertemplate="%{x|%b %Y}: %{y}% HIGH<extra></extra>")
    f.add_scatter(x=g["month"], y=me, name="MEDIUM", stackgroup="r", line=dict(color=_MED, width=1),
                  hovertemplate="%{x|%b %Y}: %{y}% MEDIUM<extra></extra>")
    f.update_yaxes(ticksuffix="%", rangemode="tozero")
    f.update_xaxes(dtick="M1", tickformat="%b<br>%Y")
    if p_start is not None and p_start > lo:
        f.add_vrect(x0=p_start - pd.Timedelta(days=15), x1=p_end.to_period("M").to_timestamp() + pd.Timedelta(days=15),
                    fillcolor="#0078C0", opacity=0.07, line_width=0,
                    annotation_text="selected period", annotation_position="top left",
                    annotation_font=dict(size=10, color="#0078C0"))
    return f


_DEEPER = {"district": ("district_hospital", "Hospital"),
           "district_hospital": ("health_facility", "Health facility")}


def _hotspots_fig(r: pd.DataFrame, level) -> go.Figure:
    if level is None:
        return _empty("Your area is a single health facility")
    col, label = level
    if r.empty or col not in r.columns:
        return _empty()
    # e.g. a district with one hospital → compare its health facilities instead
    while r[col].nunique() < 2 and col in _DEEPER:
        col, label = _DEEPER[col]
    g = (r.assign(_at=r["risk_level"].isin(["HIGH", "MEDIUM"]))
          .groupby(col, observed=True).agg(n=("_at", "size"), at=("_at", "sum")).reset_index())
    g = g[g["n"] >= 30]
    if g.empty:
        return _empty("Too few children per area")
    g["pct"] = (g["at"] / g["n"] * 100).round(1)
    g = g.sort_values("pct").tail(15)
    f = _fig(max(240, 26 * len(g)))
    f.update_layout(title=dict(text=f"by {label.lower()} · top {len(g)}", x=0, y=0.99,
                               font=dict(size=11, color="#5D6D7E")))
    f.add_bar(y=g[col].astype(str), x=g["pct"], orientation="h", marker_color=_HIGH,
              text=g["pct"].map(lambda v: f"{v}%"), textposition="outside",
              customdata=g[["at", "n"]].to_numpy(),
              hovertemplate=f"{label}: %{{y}}<br>%{{x}}% at risk (%{{customdata[0]:,}} of %{{customdata[1]:,}})<extra></extra>")
    f.update_traces(cliponaxis=False)
    f.update_xaxes(visible=False, range=[0, g["pct"].max() * 1.2])
    f.update_layout(margin=dict(l=10, r=20, t=24, b=10))
    return f


def _age_fig(r: pd.DataFrame) -> go.Figure:
    at = r[r["risk_level"].isin(["HIGH", "MEDIUM"])] if not r.empty else r
    if at.empty or "age_months" not in at.columns:
        return _empty("No at-risk children")
    grp = pd.cut(at["age_months"], _AGE_BINS, labels=_AGE_LABELS)
    t = pd.crosstab(grp, at["risk_level"].astype(object)).reindex(_AGE_LABELS).fillna(0)
    f = _fig()
    for lvl, colr in (("HIGH", _HIGH), ("MEDIUM", _MED)):
        if lvl in t.columns:
            f.add_bar(x=t.index, y=t[lvl], name=lvl, marker_color=colr,
                      hovertemplate="%{x}: %{y:,} " + lvl + "<extra></extra>")
    f.update_layout(barmode="stack")
    return f


def _table(r: pd.DataFrame) -> html.Div:
    from core.exports import _details
    from core.risk_classifier import REASONS
    at = r[r["risk_level"].isin(["HIGH", "MEDIUM"])].copy() if not r.empty else r
    if at.empty:
        return dbc.Alert("No at-risk children for this selection.", color="success", className="py-2 mb-0")
    at["_o"] = at["risk_level"].astype(object).map({"HIGH": 0, "MEDIUM": 1})
    at = at.sort_values(["_o", "weight_velocity"], na_position="last").head(100)
    d = _details(at["tracked_entity_instance"].tolist()).set_index("tracked_entity_instance")
    rows = []
    for _, x in at.iterrows():
        info = d.loc[x["tracked_entity_instance"]] if x["tracked_entity_instance"] in d.index else {}
        reasons = ", ".join(lbl for bit, lbl in REASONS.items() if int(x["risk_flags"]) & bit) or "—"
        wv = x["weight_velocity"]
        rows.append(html.Tr([
            html.Td([html.Div(info.get("child_name") or "—", className="fw-semibold"),
                     html.Div(info.get("child_id") or "", className="nhic-hint")]),
            html.Td(dbc.Badge(str(x["risk_level"]), color="danger" if x["risk_level"] == "HIGH" else "warning")),
            html.Td(reasons),
            html.Td("—" if pd.isna(wv) else f"{wv:+.2f}", className="text-end"),
            html.Td("—" if pd.isna(x["age_months"]) else f"{x['age_months']:.0f}", className="text-end"),
            html.Td(info.get("health_facility") or "—"),
            html.Td(pd.Timestamp(x["immunization_date"]).strftime("%d %b %Y")),
            html.Td(info.get("mother_phone") or "—"),
        ]))
    return html.Div(dbc.Table([
        html.Thead(html.Tr([html.Th("Child"), html.Th("Risk"), html.Th("Why"),
                            html.Th("Weight change (kg/mo)", className="text-end"),
                            html.Th("Age (mo)", className="text-end"), html.Th("Health facility"),
                            html.Th("Latest visit"), html.Th("Mother phone")])),
        html.Tbody(rows),
    ], size="sm", hover=True, className="mb-0"), className="nhic-table-wrap")


# ── Callbacks ──────────────────────────────────────────────────────────────────

def register_callbacks(app) -> None:

    @app.callback(Output("rd-dist", "options"), Input("rd-prov", "value"))
    def _dist_opts(province):
        import data
        from auth import current_user
        c = data.get_child_df()
        if c is None:
            return []
        return data.district_options(data.filter_by_user(c, (current_user() or {})), province)

    @app.callback(Output("rd-hosp", "options"), Input("rd-dist", "value"))
    def _hosp_opts(district):
        import data
        from auth import current_user
        c = data.get_child_df()
        if c is None:
            return []
        return data.hospital_options(data.filter_by_user(c, (current_user() or {})), district)

    @app.callback(Output("rd-period", "value"),
                  Output("rd-dates", "start_date"), Output("rd-dates", "end_date"),
                  Input("rd-period", "value"),
                  Input("rd-dates", "start_date"), Input("rd-dates", "end_date"),
                  State("rd-period", "options"), prevent_initial_call=True)
    def _period(period, start, end, options):
        from dash import ctx
        from pages.dashboard import period_range as _pr
        if ctx.triggered_id == "rd-period":
            if period == "all":
                rngs = [o["value"].split("|") for o in options if o["value"] != "all"]
                return no_update, min(r[0] for r in rngs), max(r[1] for r in rngs)
            if period:
                from pages.dashboard import period_range
                a, b = period_range(period)
                return no_update, a, b
            return no_update, no_update, no_update
        match = next((o["value"] for o in options
                      if o["value"] != "all" and _pr(o["value"]) == (start, end)), None)
        return match, no_update, no_update

    @app.callback(Output("rd-kpis", "children"),
                  Output("rd-reasons", "figure"), Output("rd-trend", "figure"),
                  Output("rd-hotspots", "figure"), Output("rd-age", "figure"),
                  Output("rd-table", "children"),
                  Input("rd-prov", "value"), Input("rd-dist", "value"), Input("rd-hosp", "value"),
                  Input("rd-dates", "start_date"), Input("rd-dates", "end_date"),
                  Input("rd-cohort", "value"))
    def _render(province, district, hospital, start, end, cohort):
        import data
        from auth import current_user
        user = (current_user() or {})
        risk = data.get_risk_df()
        if not user or risk is None or risk.empty:
            msg = dbc.Alert("At-risk data is still loading — refresh in a minute.",
                            color="info", className="py-2")
            return [dbc.Col(msg)], _empty(), _empty(), _empty(), _empty(), None
        r = data.scoped_risk_df(risk, user, province, district, hospital, start, end)
        if cohort == "stunted" and not r.empty and "is_stunted" in r.columns:
            r = r[r["is_stunted"].eq(True)]
        sm = data.summarize_risk(r)
        who = "stunted children weighed" if cohort == "stunted" else "children weighed"
        kpis = [
            metric_card("At-Risk Children", f"{sm['at_risk']:,}", f"{sm['pct']}% of {sm['measured']:,} {who}",
                        _HIGH, "bi-exclamation-triangle-fill", xl=True),
            metric_card("HIGH Risk", f"{sm['high']:,}", "weight loss or severe wasting",
                        "#8B1E14", "bi-heart-pulse-fill", xl=True),
            metric_card("MEDIUM Risk", f"{sm['medium']:,}", "growth faltering",
                        _MED, "bi-graph-down-arrow", xl=True),
            metric_card("Also Stunted", f"{sm['stunted_at_risk']:,}", "at risk AND stunted",
                        "#5D3A9B", "bi-arrow-down-circle-fill", xl=True),
        ]
        table = (dbc.Alert([html.I(className="bi bi-lock me-2"),
                            "Sign in to see the children to follow up."],
                           color="light", className="py-2 mb-0")
                 if user.get("public") else _table(r))
        return (kpis, _reasons_fig(sm), _trend_fig(user, province, district, hospital, start, end),
                _hotspots_fig(r, _next_level(user, province, district, hospital)), _age_fig(r),
                table)

    @app.callback(Output("rd-download", "data"), Output("rd-dl-msg", "children"),
                  Input("rd-dl-btn", "n_clicks"),
                  State("rd-prov", "value"), State("rd-dist", "value"), State("rd-hosp", "value"),
                  State("rd-dates", "start_date"), State("rd-dates", "end_date"),
                  State("rd-cohort", "value"), prevent_initial_call=True,
                  running=[(Output("rd-dl-btn", "disabled"), True, False)])
    def _download(n, province, district, hospital, start, end, cohort):
        from auth import current_user
        from core.exports import build_download, ExportTooLarge
        if not n:
            return no_update, no_update
        try:
            content, fname, rows = build_download("at_risk", (current_user() or {}), province,
                                                  district, hospital, start, end,
                                                  stunted_only=(cohort == "stunted"))
        except ExportTooLarge as exc:
            return no_update, dbc.Alert(str(exc), color="warning", dismissable=True, className="py-2")
        except Exception as exc:
            return no_update, dbc.Alert(f"Download failed: {exc}", color="danger",
                                        dismissable=True, className="py-2")
        return dcc.send_bytes(content, fname), dbc.Alert(
            f"Downloaded {rows:,} at-risk children. The file contains personal data — keep it "
            "within your team.", color="success", dismissable=True, duration=8000, className="py-2")
