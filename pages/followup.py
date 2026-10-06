"""
pages/followup.py — Follow-up recording + analytics.

Tab 1 (Search & Record):
  District → Hospital → Sector → Facility cascading dropdowns
  → children list from PostgreSQL → follow-up data entry form → upsert to DB

Tab 2 (Analytics):
  Follow-up activity trend chart from stunting_followup table
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from dash import ALL, Input, Output, State, dcc, html, ctx, no_update
import dash_bootstrap_components as dbc

_INTERVENTIONS = [
    "Therapeutic feeding",
    "Nutritional counselling",
    "Micronutrient supplementation",
    "Referral to nutrition centre",
    "Growth monitoring",
    "Deworming",
    "Vitamin A supplementation",
    "WASH promotion",
    "Other",
]
_LBL = {"fontSize": "0.78rem", "fontWeight": "600", "color": "#5D6D7E"}


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _db():
    from config.backend import get_conn
    return get_conn()


def _me() -> dict:
    from flask import session
    return session.get("user") or {}


def _and_scope(sql: str, params: list, sc: dict) -> tuple[str, list]:
    """Append the signed-in user's area to a query that already has WHERE.
    Every follow-up query goes through this — the browser's filters only
    narrow WITHIN the user's own area, never beyond it."""
    from config.backend import scope_where
    clause, extra = scope_where(_me(), sc)
    return (sql + f" AND {clause}", list(params) + extra) if clause else (sql, list(params))


def _qlist(sql: str, params=None) -> list:
    try:
        conn = _db()
        df   = pd.read_sql(sql, conn, params=params)
        conn.close()
        return df.iloc[:, 0].dropna().tolist() if not df.empty else []
    except Exception:
        return []


# ── Page layout ────────────────────────────────────────────────────────────────

def layout(user: dict) -> html.Div:
    return html.Div([
        dbc.Row([
            dbc.Col(html.H5("Follow-Up Management", className="fw-bold mb-0",
                            style={"color": "#2C3E50"})),
            dbc.Col(html.Span("Search and record follow-ups for stunted children.",
                              className="text-muted", style={"fontSize": "0.78rem"}),
                    className="text-end"),
        ], className="align-items-center mb-3"),

        dbc.Tabs([
            dbc.Tab(label="Search & Record", tab_id="search"),
            dbc.Tab(label="Analytics",       tab_id="analytics"),
        ], id="fu-tabs", active_tab="search", className="mb-3"),

        html.Div(id="fu-tab-content"),

        dcc.Store(id="fu-children-store"),
        dcc.Store(id="fu-selected-child"),
    ])


def _search_tab() -> html.Div:
    return html.Div([
        dbc.Card(dbc.CardBody([
            dbc.Row([
                dbc.Col([dbc.Label("District", style=_LBL),
                         dcc.Dropdown(id="fu-district", placeholder="Select district…",
                                      clearable=True, style={"fontSize": "0.82rem"})], md=3),
                dbc.Col([dbc.Label("Hospital", style=_LBL),
                         dcc.Dropdown(id="fu-hospital", placeholder="Select district first…",
                                      clearable=True, disabled=True,
                                      style={"fontSize": "0.82rem"})], md=3),
                dbc.Col([dbc.Label("Sector", style=_LBL),
                         dcc.Dropdown(id="fu-sector", placeholder="Select hospital first…",
                                      clearable=True, disabled=True,
                                      style={"fontSize": "0.82rem"})], md=3),
                dbc.Col([dbc.Label("Health Facility", style=_LBL),
                         dcc.Dropdown(id="fu-facility", placeholder="Select sector first…",
                                      clearable=True, disabled=True,
                                      style={"fontSize": "0.82rem"})], md=3),
            ], className="g-2 mb-3"),
            dbc.Button([html.I(className="bi bi-search me-1"), "Search Children"],
                       id="fu-search-btn", color="danger", size="sm", className="fw-semibold"),
        ]), className="border-0 shadow-sm mb-3", style={"borderRadius": "8px"}),

        dbc.Row([
            dbc.Col(html.Div(id="fu-children-list",
                             children=html.P("Use the filters above to search.",
                                             className="text-muted text-center py-3",
                                             style={"fontSize": "0.82rem"})), md=5),
            dbc.Col(html.Div(id="fu-form-area",
                             children=html.P("Select a child from the list.",
                                             className="text-muted text-center py-3",
                                             style={"fontSize": "0.82rem"})), md=7),
        ], className="g-3"),
    ])


def _form_card(child: dict) -> dbc.Card:
    age      = child.get("age_in_months", "—")
    gender   = child.get("gender", "—")
    facility = child.get("health_facility", "—")
    hospital = child.get("district_hospital", "—")

    return dbc.Card(dbc.CardBody([
        html.H6(f"Child: {child.get('tracked_entity_instance','—')}",
                className="fw-bold mb-1", style={"fontSize": "0.9rem"}),
        html.Div(f"Age: {age} mo  ·  Gender: {gender}  ·  {facility}  ·  {hospital}",
                 className="text-muted mb-3", style={"fontSize": "0.75rem"}),

        dbc.Row([
            dbc.Col([dbc.Label("Follow-Up Date", style=_LBL),
                     dbc.Input(id="fu-date", type="date", value=str(date.today()),
                               className="mb-2")], md=6),
            dbc.Col([dbc.Label("Weight (kg)", style=_LBL),
                     dbc.Input(id="fu-weight", type="number", min=0, step=0.1,
                               placeholder="e.g. 8.5", className="mb-2")], md=6),
        ]),
        dbc.Row([
            dbc.Col([dbc.Label("Height (cm)", style=_LBL),
                     dbc.Input(id="fu-height", type="number", min=0, step=0.1,
                               placeholder="e.g. 72.0", className="mb-2")], md=6),
        ]),

        dbc.Label("Interventions", style=_LBL),
        dbc.Checklist(id="fu-interventions",
                      options=[{"label": i, "value": i} for i in _INTERVENTIONS],
                      value=[], className="mb-2", style={"fontSize": "0.82rem"}),

        dbc.Label("Notes", style=_LBL),
        dbc.Textarea(id="fu-notes", placeholder="Additional clinical notes…",
                     rows=3, className="mb-3", style={"fontSize": "0.82rem"}),

        dbc.Button([html.I(className="bi bi-floppy-fill me-1"), "Save Follow-Up"],
                   id="fu-save-btn", color="danger", className="fw-semibold"),
        html.Div(id="fu-save-status", className="mt-2"),
    ]), className="border-0 shadow-sm", style={"borderRadius": "8px"})


def _analytics_content() -> html.Div:
    try:
        from config.backend import ensure_followup_table, schema, scope_where
        ensure_followup_table()
        sc = schema()
        clause, params = scope_where(_me(), sc)
        where = (f"WHERE tracked_entity_instance IN (SELECT {sc['tei']} FROM {sc['table']} "
                 f"WHERE {clause})" if clause else "")
        conn = _db()
        df   = pd.read_sql(
            f"SELECT followup_date, COUNT(*) AS count FROM stunting_followup {where} "
            "GROUP BY followup_date ORDER BY followup_date", conn, params=params or None)
        conn.close()
    except Exception as e:
        return dbc.Alert(f"Could not load analytics: {e}", color="warning")

    if df.empty:
        fig = go.Figure()
        fig.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            height=320,
            annotations=[dict(text="No follow-up records yet.", x=0.5, y=0.5,
                              xref="paper", yref="paper", showarrow=False,
                              font=dict(size=14, color="#aaa"))],
        )
    else:
        fig = px.area(df, x="followup_date", y="count",
                      labels={"followup_date": "", "count": "Follow-Ups Recorded"},
                      title="Follow-Up Activity Over Time",
                      color_discrete_sequence=["#C0392B"])
        fig.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            height=320,
            font=dict(family="Inter,system-ui,sans-serif", size=12),
            margin=dict(l=40, r=20, t=40, b=40),
        )

    return dbc.Card(
        dbc.CardBody(dcc.Graph(figure=fig, config={"displayModeBar": False},
                               style={"height": "320px"}), className="p-2"),
        className="border-0 shadow-sm", style={"borderRadius": "8px"})


# ── Callbacks ──────────────────────────────────────────────────────────────────

def register_callbacks(app) -> None:

    @app.callback(Output("fu-tab-content", "children"),
                  Input("fu-tabs", "active_tab"))
    def _switch(tab):
        return _analytics_content() if tab == "analytics" else _search_tab()

    @app.callback(Output("fu-district", "options"),
                  Input("fu-tabs", "active_tab"))
    def _load_districts(tab):
        if tab != "search":
            return []
        from config.backend import schema
        s = schema()
        sql, params = _and_scope(
            f"SELECT DISTINCT {s['district']} AS district FROM {s['table']} "
            f"WHERE {s['district']} IS NOT NULL", [], s)
        items = _qlist(sql + " ORDER BY 1", params or None)
        return [{"label": d, "value": d} for d in items]

    @app.callback(Output("fu-hospital", "options"),
                  Output("fu-hospital", "disabled"),
                  Output("fu-hospital", "value"),
                  Input("fu-district", "value"))
    def _load_hospitals(district):
        if not district:
            return [], True, None
        from config.backend import schema
        s = schema()
        sql, params = _and_scope(
            f"SELECT DISTINCT {s['hospital']} AS hospital FROM {s['table']} "
            f"WHERE {s['district']}=%s AND {s['hospital']} IS NOT NULL", [district], s)
        items = _qlist(sql + " ORDER BY 1", params)
        return [{"label": h, "value": h} for h in items], False, None

    @app.callback(Output("fu-sector", "options"),
                  Output("fu-sector", "disabled"),
                  Output("fu-sector", "value"),
                  Input("fu-hospital", "value"))
    def _load_sectors(hospital):
        if not hospital:
            return [], True, None
        from config.backend import schema
        sc = schema()
        sql, params = _and_scope(
            f"SELECT DISTINCT {sc['sector']} AS sector FROM {sc['table']} "
            f"WHERE {sc['hospital']}=%s AND {sc['sector']} IS NOT NULL", [hospital], sc)
        items = _qlist(sql + " ORDER BY 1", params)
        return [{"label": s, "value": s} for s in items], False, None

    @app.callback(Output("fu-facility", "options"),
                  Output("fu-facility", "disabled"),
                  Output("fu-facility", "value"),
                  Input("fu-sector", "value"),
                  State("fu-hospital", "value"))
    def _load_facilities(sector, hospital):
        if not sector or not hospital:
            return [], True, None
        from config.backend import schema
        sc = schema()
        sql, params = _and_scope(
            f"SELECT DISTINCT {sc['facility']} AS facility FROM {sc['table']} "
            f"WHERE {sc['hospital']}=%s AND {sc['sector']}=%s "
            f"AND {sc['facility']} IS NOT NULL", [hospital, sector], sc)
        items = _qlist(sql + " ORDER BY 1", params)
        return [{"label": f, "value": f} for f in items], False, None

    @app.callback(
        Output("fu-children-store", "data"),
        Output("fu-children-list",  "children"),
        Input("fu-search-btn", "n_clicks"),
        State("fu-district",   "value"),
        State("fu-hospital",   "value"),
        State("fu-sector",     "value"),
        State("fu-facility",   "value"),
        prevent_initial_call=True,
    )
    def _search(_, district, hospital, sector, facility):
        from config.backend import schema
        sc = schema()
        sql    = (f"SELECT DISTINCT {sc['tei']} AS tracked_entity_instance, "
                  f"{sc['gender']} AS gender, {sc['age']} AS age_in_months, "
                  f"{sc['facility']} AS health_facility, "
                  f"{sc['hospital']} AS district_hospital "
                  f"FROM {sc['table']} WHERE {sc['stunted_pred']}")
        params = []
        if district: sql += f" AND {sc['district']}=%s";  params.append(district)
        if hospital: sql += f" AND {sc['hospital']}=%s";  params.append(hospital)
        if sector:   sql += f" AND {sc['sector']}=%s";    params.append(sector)
        if facility: sql += f" AND {sc['facility']}=%s";  params.append(facility)
        sql, params = _and_scope(sql, params, sc)          # never beyond the user's area
        sql += " ORDER BY age_in_months LIMIT 200"
        try:
            conn = _db()
            df   = pd.read_sql(sql, conn, params=params or None)
            conn.close()
            children = df.where(pd.notna(df), None).to_dict(orient="records")
        except Exception as e:
            return [], dbc.Alert(f"DB error: {e}", color="danger", className="py-2")

        if not children:
            return [], html.P("No stunted children found for these filters.",
                              className="text-muted", style={"fontSize": "0.82rem"})

        items = [
            html.Div(
                html.Button([
                    html.Div(c.get("tracked_entity_instance", "—"),
                             style={"fontSize": "0.8rem", "fontWeight": "600"}),
                    html.Div(f"{c.get('gender','—')} · {c.get('age_in_months','—')} mo "
                             f"· {c.get('health_facility','—')}",
                             style={"fontSize": "0.7rem", "color": "#5D6D7E"}),
                ],
                    id={"type": "fu-child-btn", "index": i}, n_clicks=0,
                    style={"width": "100%", "textAlign": "left", "background": "#fff",
                           "border": "1px solid #dee2e6", "borderRadius": "6px",
                           "padding": "8px 10px", "cursor": "pointer"},
                ),
                className="mb-1",
            )
            for i, c in enumerate(children)
        ]

        return children, html.Div([
            html.Div(f"{len(children)} stunted child{'ren' if len(children)!=1 else ''} found:",
                     className="text-muted mb-2", style={"fontSize": "0.75rem"}),
            html.Div(items, style={"maxHeight": "480px", "overflowY": "auto"}),
        ])

    @app.callback(
        Output("fu-selected-child", "data"),
        Output("fu-form-area",      "children"),
        Input({"type": "fu-child-btn", "index": ALL}, "n_clicks"),
        State("fu-children-store", "data"),
        prevent_initial_call=True,
    )
    def _select_child(n_list, children):
        if not any(n_list) or not children:
            return no_update, no_update
        trig = ctx.triggered[0]["prop_id"]
        try:
            idx = json.loads(trig.split(".")[0])["index"]
        except Exception:
            return no_update, no_update
        child = children[idx]
        return child, _form_card(child)

    @app.callback(
        Output("fu-save-status", "children"),
        Input("fu-save-btn",        "n_clicks"),
        State("fu-selected-child",  "data"),
        State("fu-date",            "value"),
        State("fu-weight",          "value"),
        State("fu-height",          "value"),
        State("fu-interventions",   "value"),
        State("fu-notes",           "value"),
        prevent_initial_call=True,
    )
    def _save(n, child, fdate, weight, height, interventions, notes):
        me = _me()
        if not me or me.get("readonly"):
            return dbc.Alert("View-only access can't record follow-ups.", color="warning",
                             className="py-2")
        if not child or not child.get("tracked_entity_instance"):
            return dbc.Alert("No child selected.", color="warning", className="py-2")
        try:
            from config.backend import ensure_followup_table, schema
            ensure_followup_table()
            sc  = schema()
            tei = child.get("tracked_entity_instance")
            conn = _db()
            cur  = conn.cursor()
            # The child comes from the browser — confirm on the server that it
            # really belongs to this user's area before writing anything.
            sql, params = _and_scope(f"SELECT 1 FROM {sc['table']} WHERE {sc['tei']} = %s",
                                     [tei], sc)
            cur.execute(sql + " LIMIT 1", params)
            if cur.fetchone() is None:
                conn.close()
                return dbc.Alert("This child is outside your area.", color="danger", className="py-2")
            cur.execute(
                "INSERT INTO stunting_followup "
                "(tracked_entity_instance, followup_date, weight_kg, height_cm, "
                " interventions, notes, created_at, recorded_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,NOW(),%s) "
                "ON CONFLICT (tracked_entity_instance, followup_date) DO UPDATE "
                "SET weight_kg=EXCLUDED.weight_kg, height_cm=EXCLUDED.height_cm, "
                "    interventions=EXCLUDED.interventions, notes=EXCLUDED.notes, "
                "    recorded_by=EXCLUDED.recorded_by",
                (tei, fdate, weight, height,
                 json.dumps(interventions or []), notes or "", me.get("username")),
            )
            conn.commit()
            conn.close()
            return dbc.Alert("✅ Follow-up saved.", color="success", className="py-2")
        except Exception as e:
            return dbc.Alert(f"Error: {e}", color="danger", className="py-2")
