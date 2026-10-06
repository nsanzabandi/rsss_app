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

    # Filter options + earliest date come from the in-memory per-child table —
    # NOT get_df(), which reads ~0.5M rows from the database (~14s) and used to
    # block the page from opening. get_df() is only a fallback on a cold start.
    from data import get_child_df, get_visits_df
    src = get_child_df()
    if src is None:
        src = get_df()
    df_u   = filter_by_user(src, user) if src is not None else pd.DataFrame()
    role   = user.get("role", "")
    locked = role in ("hospital", "health_center")

    d_end   = datetime.today().strftime("%Y-%m-%d")
    visits  = get_visits_df()
    d_start = (visits["immunization_date"].iloc[0].strftime("%Y-%m-%d")     # date-sorted
               if visits is not None and not visits.empty else "2020-01-01")

    # Opens on LAST MONTH (complete) compared with the month before — both
    # months have (nearly) all their records, so the ▲/▼ are always meaningful.
    # "This month" (fills in as records arrive), quarters, years, All time: Period.
    _m0 = pd.Timestamp.today().normalize().replace(day=1)
    _lm = _m0.to_period("M") - 1
    _def_start, _def_end = f"{_lm.start_time:%Y-%m-%d}", f"{_lm.end_time:%Y-%m-%d}"
    _default = {"value": f"{_def_start}|{_def_end}"}
    return html.Div([
        # Page title lives in the top bar and sync freshness in the sidebar
        # (components/layout.py), so the page opens straight on the filters.
        dcc.Download(id="db-download"),

        # Filters — the Period picker fills the date range; the dashboard and
        # the downloads both follow the dates.
        dbc.Card(dbc.CardBody(
            dbc.Row([
                dbc.Col(dcc.Dropdown(id="db-prov",  placeholder="All Provinces",
                                     options=province_options(df_u), clearable=True,
                                     disabled=locked, style={"fontSize": "0.82rem"}),
                        xs=6, md=4, xl=2),
                dbc.Col(dcc.Dropdown(id="db-dist",  placeholder="All Districts",
                                     options=district_options(df_u), clearable=True,
                                     disabled=locked, style={"fontSize": "0.82rem"}),
                        xs=6, md=4, xl=2),
                dbc.Col(dcc.Dropdown(id="db-hosp",  placeholder="All Hospitals",
                                     options=hospital_options(df_u), clearable=True,
                                     disabled=locked, style={"fontSize": "0.82rem"}),
                        xs=6, md=4, xl=2),
                dbc.Col(dcc.Dropdown(id="db-period", options=_period_options(d_start),
                                     value=_default["value"], clearable=False, searchable=False,
                                     placeholder="Custom dates",
                                     style={"fontSize": "0.82rem"}),
                        xs=6, md=4, xl=2),
                dbc.Col(dcc.DatePickerRange(id="db-dates",
                                            start_date=_def_start, end_date=_def_end,
                                            display_format="DD/MM/YYYY",
                                            style={"fontSize": "0.82rem"}),
                        xs=12, md=True),
                dbc.Col(_download_menu(user), width="auto",
                        className="d-flex justify-content-end ms-auto"),
            ], className="g-2 align-items-center"),
            className="py-2 px-3"),
            className="mb-2 border-0 shadow-sm", style={"borderRadius": "8px"}),
        html.Div(id="db-dl-msg", className="mb-1"),

        # KPI row (national headline — computed once, warmed in background)
        html.Div(id="db-kpi-caption", hidden=True),   # explanation now lives on the cards
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


_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _last3_option() -> dict:
    """The last 3 COMPLETE months (today: Jul–Sep), the dashboard's default
    view. Value is tagged "last3|start|end" so it stays distinct from a
    quarter covering the same months."""
    m0 = pd.Timestamp.today().normalize().replace(day=1)
    a, b = (m0.to_period("M") - 3).to_timestamp(), m0 - pd.Timedelta(days=1)
    return {"label": f"Last 3 months ({_MON[a.month-1]}–{_MON[b.month-1]} {b.year})",
            "value": f"last3|{a:%Y-%m-%d}|{b:%Y-%m-%d}"}


def period_range(value: str) -> tuple[str, str]:
    """Start/end dates of a Period option value ("start|end" or "last3|start|end")."""
    parts = value.split("|")
    return parts[-2], parts[-1]


def _period_options(data_start: str) -> list[dict]:
    """Quick periods → date ranges. Value = "YYYY-MM-DD|YYYY-MM-DD" ("all" for
    everything). Quarters are labelled by their months, so they read right in
    either calendar or fiscal (Jul–Jun) terms."""
    today = pd.Timestamp.today().normalize()
    first = pd.Timestamp(data_start or "2020-01-01")
    def rng(a, b):
        return f"{a:%Y-%m-%d}|{min(b, today):%Y-%m-%d}"
    month0 = today.replace(day=1)
    prev0 = (month0 - pd.Timedelta(days=1)).replace(day=1)
    opts = [{"label": "All time", "value": "all"},
            {"label": f"This month ({_MON[today.month-1]} {today.year})",
             "value": rng(month0, today)},
            {"label": f"Last month ({_MON[prev0.month-1]} {prev0.year})",
             "value": rng(prev0, month0 - pd.Timedelta(days=1))},
            _last3_option()]
    q = pd.Period(today, freq="Q")
    while q.end_time >= first:
        a, b = q.start_time.normalize(), q.end_time.normalize()
        tag = " (so far)" if b >= today else ""
        opts.append({"label": f"{_MON[a.month-1]}–{_MON[b.month-1]} {a.year}{tag}",
                     "value": rng(a, b)})
        q -= 1
    for y in range(today.year, first.year - 1, -1):
        opts.append({"label": f"Year {y}" + (" (so far)" if y == today.year else ""),
                     "value": rng(pd.Timestamp(y, 1, 1), pd.Timestamp(y, 12, 31))})
    return opts


_DL_ITEMS = [("stunted", "bi-arrow-down-circle", "Stunted children"),
             ("at_risk", "bi-exclamation-triangle", "At-risk children"),
             ("missed", "bi-calendar-x", "Missed appointments"),
             ("assessed", "bi-people", "All assessed children")]


def _download_menu(user: dict):
    """Excel downloads limited to the user's catchment + the filters below.
    Hidden for view-only email links."""
    if user.get("readonly"):
        return None
    from core.exports import scope_label
    return dbc.DropdownMenu(
        [dbc.DropdownMenuItem(f"Your area: {scope_label(user)} · uses the filters below",
                              header=True, style={"fontSize": "0.72rem"})]
        + [dbc.DropdownMenuItem([html.I(className=f"bi {icon} me-2"), label],
                                id=f"db-dl-{key}", n_clicks=0)
           for key, icon, label in _DL_ITEMS],
        id="db-dl-menu", label=[html.I(className="bi bi-download me-1"), "Download list"],
        size="sm", color="light", align_end=True, className="nhic-dl-menu")


# ── Callbacks ──────────────────────────────────────────────────────────────────

def _month_aligned(a: pd.Timestamp, b: pd.Timestamp) -> bool:
    return a.day == 1 and (b + pd.Timedelta(days=1)).day == 1


def _prev_range(start, end) -> tuple[str, str, str] | None:
    """The period just before [start, end], same length. Whole months/quarters/
    years shift by calendar months (Jul–Sep → Apr–Jun, Sep → Aug); anything
    else shifts by the same number of days. None for "All time"-sized ranges."""
    if not start or not end:
        return None
    a, b = pd.Timestamp(start), pd.Timestamp(end)
    if (b - a).days > 400:                                  # ~all time: nothing to compare
        return None
    if _month_aligned(a, b):
        n = (b.year - a.year) * 12 + b.month - a.month + 1
        pa = (a.to_period("M") - n).to_timestamp()
        pb = a - pd.Timedelta(days=1)
        if n == 1:
            label = f"{pa:%b %Y}"
        elif n == 12 and pa.month == 1:
            label = f"{pa.year}"
        else:
            label = f"{pa:%b}–{pb:%b %Y}"
    elif a.day == 1 and a.to_period("M") == b.to_period("M"):
        # month in progress (1–6 Oct) → the same days of last month (1–6 Sep)
        pm = (a.to_period("M") - 1)
        pa = pm.to_timestamp()
        pb = pa + pd.Timedelta(days=min(b.day, pm.days_in_month) - 1)
        label = f"1–{pb.day} {pb:%b %Y}"
    elif a.month == 1 and a.day == 1 and (b - a).days > 92:
        # year so far → the same dates last year
        pa, pb = a - pd.DateOffset(years=1), b - pd.DateOffset(years=1)
        label = f"same dates {pa.year}"
    else:
        pb = a - pd.Timedelta(days=1)
        pa = pb - (b - a)
        label = f"{pa:%d %b}–{pb:%d %b %Y}"
    return f"{pa:%Y-%m-%d}", f"{pb:%Y-%m-%d}", label


def _change(cur, prev, vs: str, points: bool = False, cur_label: str | None = None) -> dict | None:
    """Change vs the previous period, spelled out so it can't be misread:
    counts → "+3,888 (+9.2%)" with "vs Apr–Jun 2026: 42,405";
    rates  → "+0.9 pts" with "vs Apr–Jun 2026: 11.6%".
    cur_label: when the badge compares a different window than the card shows
    (All time → "2026 so far"), name it and give its value too."""
    if cur is None or prev is None:
        return None
    if points:
        diff = round(float(cur) - float(prev), 1)
        text = f"{diff:+.1f} pts"
        fmt = lambda v: f"{v}%"
    else:
        if prev == 0:
            return None
        diff_n = cur - prev
        diff = round(diff_n / prev * 100, 1)
        text = f"{diff_n:+,} ({diff:+.1f}%)"
        fmt = lambda v: f"{v:,}"
    direction = "up" if diff > 0 else "down" if diff < 0 else "flat"
    detail = (f"{cur_label}: {fmt(cur)} vs {vs}: {fmt(prev)}" if cur_label
              else f"vs {vs}: {fmt(prev)}")
    return {"text": text, "dir": direction, "detail": detail}


def _period_label(start, end) -> str:
    a, b = pd.Timestamp(start), pd.Timestamp(end)
    if a.day == 1 and a.to_period("M") == b.to_period("M") and not _month_aligned(a, b):
        return f"{a:%B %Y} so far (1–{b.day} {b:%b})"
    if _month_aligned(a, b):
        return f"{a:%b %Y}" if a.to_period("M") == b.to_period("M") else (
            f"{a:%b}–{b:%b %Y}" if a.year == b.year else f"{a:%b %Y}–{b:%b %Y}")
    return f"{a:%d %b %Y} – {b:%d %b %Y}"


def _risk_href(province, district, hospital, start, end) -> str:
    from urllib.parse import urlencode
    q = {k: v for k, v in (("prov", province), ("dist", district), ("hosp", hospital),
                           ("from", start), ("to", end)) if v}
    return "/rsss_app/risk" + (f"?{urlencode(q)}" if q else "")


def _national_cards(res: dict, risk: dict | None = None, href: str | None = None,
                    prev: dict | None = None, prev_risk: dict | None = None,
                    vs: str | None = None, cur: dict | None = None,
                    cur_risk: dict | None = None, cur_label: str | None = None,
                    latest_month: str | None = None, incomplete: str | None = None) -> list:
    """cur/cur_risk: the figures the ▲/▼ badge compares against prev — by
    default the card's own value; on "All time" it's this year so far vs the
    same dates last year, while the card itself shows the all-time total."""
    cur, cur_risk = cur or res, cur_risk or risk
    # When far fewer children are in the data than in the comparison period
    # (late data entry / not yet synced), comparing COUNTS would show a false
    # "improvement" — show a neutral note instead. Rates are kept.
    waiting = {"text": "data still arriving", "dir": "flat", "detail": incomplete} if incomplete else None
    """The 7 headline KPI cards (national figures, deduplicated by child)."""
    def f(v):
        return f"{v:,}" if isinstance(v, int) else ("—" if v is None else str(v))

    return [
        metric_card("Total Vaccinated", f(res["total_vaccinated"]),
                    "unique children", C_INFO, "bi-people-fill", xl=True),
        metric_card("Total Stunted", f(res["current_total"]),
                    "current (latest visit)", C_PRIMARY, "bi-arrow-down-circle-fill", xl=True,
                    delta=waiting or (prev and _change(cur["current_total"], prev["current_total"], vs, cur_label=cur_label))),
        metric_card("Ever Stunted", f(res["ever_stunted"]),
                    "stunted at any visit", C_WARNING, "bi-clock-history", xl=True,
                    delta=waiting or (prev and _change(cur["ever_stunted"], prev["ever_stunted"], vs, cur_label=cur_label))),
        metric_card("Stunting Rate", f"{res['stunting_pct']}%",
                    "of assessed children" + (f" · latest month {latest_month}" if latest_month else ""),
                    C_DANGER, "bi-graph-down-arrow", xl=True,
                    delta=prev and _change(cur["stunting_pct"], prev["stunting_pct"], vs, points=True, cur_label=cur_label)),
        metric_card("High-Risk Children",
                    f(risk["high"]) if risk else "—",
                    (f"weight loss / severe wasting · {risk['at_risk']:,} at risk in total  ›  View"
                     if risk else "growth faltering"),
                    "#B03A2E", "bi-exclamation-triangle-fill", href=href, xl=True,
                    delta=waiting or (cur_risk and prev_risk and _change(cur_risk["high"], prev_risk["high"], vs, cur_label=cur_label))),
    ]


def _options_source():
    """Area names for the filter dropdowns: the in-memory per-child table (instant);
    get_df() (a ~14s database read) only on a cold start."""
    from data import get_child_df
    c = get_child_df()
    return c if c is not None else get_df()


def register_callbacks(app) -> None:

    @app.callback(Output("db-period", "value"),
                  Output("db-dates", "start_date"), Output("db-dates", "end_date"),
                  Input("db-period", "value"),
                  Input("db-dates", "start_date"), Input("db-dates", "end_date"),
                  State("db-period", "options"),
                  prevent_initial_call=True)
    def _period(period, start, end, options):
        from dash import ctx
        if ctx.triggered_id == "db-period":
            if period == "all":
                # the real data range — exactly what the page opens with
                from data import get_visits_df
                v = get_visits_df()
                first = (v["immunization_date"].iloc[0].strftime("%Y-%m-%d")
                         if v is not None and not v.empty else "2020-01-01")
                return no_update, first, pd.Timestamp.today().strftime("%Y-%m-%d")
            a, b = period_range(period)
            return no_update, a, b
        # dates edited by hand → show which preset (if any) they match
        match = next((o["value"] for o in options
                      if o["value"] != "all" and period_range(o["value"]) == (start, end)), None)
        return match, no_update, no_update

    @app.callback(Output("db-download", "data"), Output("db-dl-msg", "children"),
                  [Input(f"db-dl-{k}", "n_clicks") for k, _, _ in _DL_ITEMS],
                  State("db-prov", "value"), State("db-dist", "value"), State("db-hosp", "value"),
                  State("db-dates", "start_date"), State("db-dates", "end_date"),
                  prevent_initial_call=True,
                  running=[(Output("db-dl-menu", "disabled"), True, False),
                           (Output("db-dl-menu", "label"),
                            [html.Span(className="spinner-border spinner-border-sm me-1"), "Preparing…"],
                            [html.I(className="bi bi-download me-1"), "Download list"])])
    def _download(*args):
        from dash import ctx
        from auth import current_user
        from core.exports import build_download, ExportTooLarge
        prov, dist, hosp, start, end = args[-5:]
        kind = (ctx.triggered_id or "").replace("db-dl-", "")
        if not any(args[:-5]):
            return no_update, no_update
        user = (current_user() or {})
        try:
            content, fname, n = build_download(kind, user, prov, dist, hosp, start, end)
        except ExportTooLarge as exc:
            return no_update, dbc.Alert(str(exc), color="warning", dismissable=True, className="py-2")
        except Exception as exc:
            return no_update, dbc.Alert(f"Download failed: {exc}", color="danger",
                                        dismissable=True, className="py-2")
        note = dbc.Alert(f"Downloaded {n:,} children. The file contains personal data — "
                         "keep it within your team.", color="success", dismissable=True,
                         duration=8000, className="py-2")
        return dcc.send_bytes(content, fname), note

    @app.callback(Output("db-dist", "options"),
                  Input("db-prov", "value"))
    def _dist_opts(province):
        from auth import current_user
        user = current_user()
        if not user:
            return []
        df = _options_source()
        return district_options(filter_by_user(df, user) if df is not None else pd.DataFrame(), province)

    @app.callback(Output("db-hosp", "options"),
                  Input("db-dist", "value"),
                  State("db-prov", "value"))
    def _hosp_opts(district, province):
        from auth import current_user
        user = current_user()
        if not user:
            return []
        df = _options_source()
        if df is None:
            return []
        df = filter_by_user(df, user)
        if province and "province" in df.columns:
            df = df[df["province"] == province]
        return hospital_options(df, district)

    @app.callback(
        Output("db-kpi-row", "children"),
        Output("db-kpi-caption", "children"),
        Output("db-kpi-poll", "disabled"),
        Input("db-kpi-poll", "n_intervals"),
        Input("db-prov",  "value"), Input("db-dist",  "value"),
        Input("db-hosp",  "value"), Input("db-dates", "start_date"),
        Input("db-dates", "end_date"),
    )
    def _kpis(_n, province, district, hospital, start, end):
        from auth import current_user
        user = current_user()
        if not user:
            return [kpi_placeholder() for _ in range(8)], "", True
        from data import (get_child_df, get_visits_df, warm_child_level,
                          child_build_status, get_risk_df)
        child  = get_child_df()
        visits = get_visits_df()
        if child is None or visits is None:
            warm_child_level()                            # ensure it's running
            keep_polling = child_build_status() != "error"
            return [kpi_placeholder() for _ in range(8)], "", (not keep_polling)
        # Cheap for the default/wide range; only re-derives from raw visits
        # (correctly, per-period) when the date range is narrow — see
        # scoped_child_df's docstring.
        from data import memo_summary
        res = memo_summary("child", user, province, district, hospital, start, end)
        risk = get_risk_df()
        have_risk = risk is not None and not risk.empty
        rs = memo_summary("risk", user, province, district, hospital, start, end) if have_risk else None
        prev = prev_rs = vs = cur = cur_rs = None
        pr = _prev_range(start, end)
        if pr is None:
            # "All time": the badge compares THIS YEAR SO FAR with the same
            # dates last year (the card still shows the all-time total).
            today = pd.Timestamp.today().normalize()
            y0 = pd.Timestamp(today.year, 1, 1)
            c_start, c_end = f"{y0:%Y-%m-%d}", f"{today:%Y-%m-%d}"
            cur = memo_summary("child", user, province, district, hospital, c_start, c_end)
            if have_risk:
                cur_rs = memo_summary("risk", user, province, district, hospital, c_start, c_end)
            ly = today - pd.DateOffset(years=1)
            pr = (f"{y0 - pd.DateOffset(years=1):%Y-%m-%d}", f"{ly:%Y-%m-%d}",
                  f"same dates {ly.year}")
            cur_label = f"{today.year} so far"
            caption = (f"All-time totals. Arrows compare {today.year} so far (1 Jan – {today:%d %b}) "
                       f"with the same dates in {ly.year}.")
        else:
            cur_label = None
            caption = f"{_period_label(start, end)}"
        if pr:
            p_start, p_end, vs = pr
            prev = memo_summary("child", user, province, district, hospital, p_start, p_end)
            if have_risk:
                prev_rs = memo_summary("risk", user, province, district, hospital, p_start, p_end)
            # Too little data in the earlier period (e.g. 2024, before the sync
            # started) makes % changes meaningless (+262,000%) → no arrows.
            base = (cur or res).get("total_vaccinated", 0)
            if prev.get("total_vaccinated", 0) < max(100, 0.2 * base):
                prev = prev_rs = None
            if cur_label is None:
                caption += f" compared with {vs} (the period just before)."
        if prev is None and cur_label is None:
            caption += " — no earlier period with enough data to compare."
        incomplete = None
        if prev:
            ratio = (cur or res).get("assessed", 0) / max(prev.get("assessed", 0), 1)
            if ratio < 0.7:
                incomplete = f"{ratio:.0%} of {vs}'s volume so far"
                caption += (f" Only {ratio:.0%} as many children are in the data as for {vs} — "
                            "records are still being entered/synced, so counts aren't compared yet; "
                            "the rate is.")
        caption += " Each child is counted once, at their latest visit in the period."
        # latest complete month's rate, to tie the card to the trend chart
        from data import get_monthly_df, monthly_rate, filter_geo as _fg
        latest = None
        mdf = get_monthly_df()
        if mdf is not None and not mdf.empty:
            mr = monthly_rate(_fg(filter_by_user(mdf, user), province, district, hospital), start, end)
            if not mr.empty:
                last = mr.iloc[-1]
                latest = f"{last['month']:%b} {last['y']}%"
        return (_national_cards(res, rs, _risk_href(province, district, hospital, start, end),
                                prev, prev_rs, vs, cur, cur_rs, cur_label, latest, incomplete),
                html.Span([html.I(className="bi bi-info-circle me-1"), caption]), True)

    @app.callback(
        Output("db-tab-content", "children"),
        Input("db-tabs",  "active_tab"),
        Input("db-prov",  "value"), Input("db-dist",  "value"),
        Input("db-hosp",  "value"), Input("db-dates", "start_date"),
        Input("db-dates", "end_date"),
        Input("db-kpi-poll", "n_intervals"),   # re-render when the table finishes building
    )
    def _tab(tab, province, district, hospital, start, end, _poll):
        from auth import current_user
        user = current_user()
        if not user:
            return html.Div()
        # Overview needs the database-read table only for its vaccination
        # coverage section → never wait for it (~14s): use it if loaded, else it
        # loads in the background and that section fills in on the next refresh.
        # The other tabs are built entirely from it, so they wait as before.
        if tab == "overview":
            from data import get_df_if_ready
            df = get_df_if_ready()
        else:
            df = get_df()
            if df is None or df.empty:
                return dbc.Alert("Data unavailable — check data/combined_df.csv",
                                 color="warning", className="mt-3")
        if df is not None:
            df = filter_by_user(df, user)
            df = filter_geo(df, province, district, hospital)
            df = filter_by_date(df, start, end)

        # The Overview now uses the cached computed per-child table (+ monthly
        # trend), filtered identically. These reflect the WHO-computed stunting
        # so the charts match the KPI cards.
        if tab == "overview":
            from data import get_child_df, get_visits_df, get_monthly_df, get_schedule_df
            child_raw = get_child_df()
            visits    = get_visits_df()
            monthly   = get_monthly_df()
            schedule  = get_schedule_df()
            child = pd.DataFrame()
            if child_raw is not None and not child_raw.empty:
                from data import memo_scoped          # same result the KPI cards just computed
                child = memo_scoped("child", user, province, district, hospital, start, end)
            # User's own area first (a district officer must see their district's
            # trend, not the national one), then the dashboard filters.
            if monthly is not None and not monthly.empty:
                monthly = filter_geo(filter_by_user(monthly, user), province, district, hospital)
            if schedule is not None and not schedule.empty:
                schedule = filter_geo(filter_by_user(schedule, user), province, district, hospital)
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

    # Trend charts show at least 12 months up to the period end, so a single
    # month/quarter is seen in context (the selected period is shaded).
    ctx_start = start
    if start and end:
        ctx = (pd.Timestamp(end).to_period("M") - 11).to_timestamp()
        ctx_start = min(pd.Timestamp(start), ctx).strftime("%Y-%m-%d")

    # ── 1. Stunting RATE over time (WHO computed, future/thin months removed) ──
    fig_rate = _empty(building if not have_month else "No date data")
    if have_month:
        m = _month_rate(monthly, ctx_start, end, "rate")
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
            if start and ctx_start != start:
                fig_rate.add_vrect(
                    x0=pd.Timestamp(start) - pd.Timedelta(days=15),
                    x1=pd.Timestamp(end).to_period("M").to_timestamp() + pd.Timedelta(days=15),
                    fillcolor="#0078C0", opacity=0.07, line_width=0,
                    annotation_text="selected period", annotation_position="top left",
                    annotation_font=dict(size=10, color="#0078C0"))
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

    # ── 3. Stunted children by sex (computed) + severe-share trend ────────────
    # Among ASSESSED children only (usable height + age) — this also keeps out
    # the ~220k 12-year-old girls recorded for HPV, who have no height.
    fig_sev = _empty(building if not have_child else "No sex recorded")
    if have_child and "sex" in child.columns:
        a = child[child["is_stunted"].notna() & child["sex"].isin(["Male", "Female"])]
        if not a.empty:
            g = (a.assign(_st=a["is_stunted"].eq(True))
                  .groupby("sex", observed=True).agg(assessed=("_st", "size"), stunted=("_st", "sum"))
                  .reindex(["Male", "Female"]).fillna(0))
            g["rate"] = (g["stunted"] / g["assessed"].clip(lower=1) * 100).round(1)
            labels = {"Male": "Boys", "Female": "Girls"}
            if g["stunted"].sum() > 0:
                fig_sev = go.Figure(go.Pie(
                    labels=[labels[x] for x in g.index], values=g["stunted"].astype(int),
                    hole=0.55, sort=False, direction="clockwise",
                    marker=dict(colors=["#1F6FB2", "#C2185B"]),
                    customdata=g[["assessed", "rate"]].to_numpy(),
                    texttemplate="%{label}<br>%{percent}", textfont=dict(size=12),
                    hovertemplate=("%{label}: %{value:,} stunted (%{percent} of stunted)<br>"
                                   "stunting rate %{customdata[1]}% of %{customdata[0]:,} assessed"
                                   "<extra></extra>")))
                rates = "   ".join(f"{labels[x]} <b>{g.loc[x, 'rate']}%</b>" for x in g.index)
                fig_sev.update_layout(**_base(height=_H), title="Stunted Children by Sex",
                                      showlegend=False)
                fig_sev.add_annotation(text=f"{int(g['stunted'].sum()):,}<br><span style='font-size:11px'>stunted</span>",
                                       x=0.5, y=0.5, showarrow=False, font=dict(size=18, color="#1F2D3D"))
                fig_sev.add_annotation(text=f"Stunting rate:   {rates}", x=0.5, y=-0.12,
                                       xref="paper", yref="paper", showarrow=False,
                                       font=dict(size=12, color="#5D6D7E"))
                fig_sev.update_layout(margin=dict(l=20, r=20, t=48, b=48))

    fig_sevtrend = _empty(building if not have_month else "No date data")
    if have_month:
        ms = _month_rate(monthly, ctx_start, end, "severe_share")
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
    fig_cov = _empty("Vaccine columns not in current data source" if df is not None
                     else "Loading vaccination data… (refresh in a moment)")
    cov = _coverage_table(df) if df is not None else pd.DataFrame()
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
