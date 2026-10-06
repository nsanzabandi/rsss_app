"""
pages/settings.py — NHIC admin settings (ministry role, not view-only).

Tabs:
  1. Users & roles      — create/edit users, reset passwords, activate/deactivate
  2. Report recipients  — hospital + national email contacts (config/*.json)
  3. Schedules          — monthly report auto-send; eTracker sync status
  4. System status      — data freshness per district, cache, database, email test

Every callback re-checks the session user: Dash callbacks are reachable by
direct POST even when the page itself isn't shown, so hiding the page is not
access control on its own.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import dash_bootstrap_components as dbc
from dash import Input, Output, State, dash_table, dcc, html, no_update

BASE_DIR        = Path(__file__).resolve().parent.parent
HOSPITALS_JSON  = BASE_DIR / "config" / "hospital_emails.json"
NATIONAL_JSON   = BASE_DIR / "config" / "national_report_recipients.json"

_ROLES = [("ministry", "Ministry (national, admin)"), ("district", "District officer"),
          ("hospital", "Hospital"), ("health_center", "Health centre")]
_EMAIL_RE    = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")
_NEW         = "__new__"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _user() -> dict | None:
    from flask import session
    return session.get("user")


def _is_admin() -> bool:
    u = _user() or {}
    return u.get("role") == "ministry" and not u.get("readonly")


def _write_json(path: Path, obj: dict) -> None:
    """Write via temp file + rename so a crash never leaves half a file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def _read_json(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


_geo_cache: dict = {}
_GEO_TTL = 600   # seconds


def _geo_options() -> dict:
    """District → hospital → health-centre hierarchy for the scope dropdowns,
    using the same expressions data.py exposes as district / district_hospital
    / health_facility (what filter_by_user() matches a user's scope against).

    Each health centre is assigned to the hospital holding most of its
    records — a handful of facilities have 1-record strays under a second
    hospital (data-entry noise) that must not widen a catchment.
    Cached for 10 minutes.
    """
    import time
    if _geo_cache.get("ts", 0) > time.time() - _GEO_TTL:
        return _geo_cache["data"]
    out = {"district": [], "district_hospital": [], "health_facility": [],
           "hosp_district": {}, "fac_hospital": {}}
    try:
        from config.db_local import get_local_conn
        conn = get_local_conn(); cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(h_district, residence_district, district_source),
                   h_district_hospital, health_facility, COUNT(*)
            FROM immunization_vaccination
            GROUP BY 1, 2, 3""")
        rows = cur.fetchall()
        cur.close(); conn.close()
        best_fac: dict[str, tuple[int, str]] = {}
        best_hosp: dict[str, tuple[int, str]] = {}
        districts = set()
        for d, h, f, n in rows:
            if d:
                districts.add(d)
            if h and d and n > best_hosp.get(h, (0, ""))[0]:
                best_hosp[h] = (n, d)
            if f and h and n > best_fac.get(f, (0, ""))[0]:
                best_fac[f] = (n, h)
        out["hosp_district"] = {h: d for h, (_, d) in best_hosp.items()}
        out["fac_hospital"]  = {f: h for f, (_, h) in best_fac.items()}
        out["district"]          = sorted(districts)
        out["district_hospital"] = sorted(out["hosp_district"])
        out["health_facility"]   = sorted(out["fac_hospital"])
        _geo_cache.update(ts=time.time(), data=out)
    except Exception as exc:
        print(f"[settings] scope lists unavailable: {exc}")
    return out


def _hospitals_in(geo: dict, district: str | None) -> list[str]:
    return [h for h in geo["district_hospital"]
            if not district or geo["hosp_district"].get(h) == district]


def _facilities_in(geo: dict, district: str | None, hospital: str | None) -> list[str]:
    return [f for f in geo["health_facility"]
            if (not hospital or geo["fac_hospital"].get(f) == hospital)
            and (not district or geo["hosp_district"].get(geo["fac_hospital"].get(f)) == district)]


def _opts(values: list[str]) -> list[dict]:
    return [{"label": v, "value": v} for v in values]


def _card(title: str, icon: str, body, hint: str | None = None) -> dbc.Card:
    head = [html.Div([html.I(className=f"bi {icon}"), title], className="nhic-card-title")]
    if hint:
        head.append(html.Div(hint, className="nhic-hint mt-1"))
    return dbc.Card(dbc.CardBody(head + [html.Div(body, className="mt-3")]),
                    className="nhic-card mb-3")


def _lbl(text: str) -> dbc.Label:
    return dbc.Label(text, className="nhic-hint fw-semibold mb-1")


def _msg(text: str, color: str = "success") -> dbc.Alert:
    return dbc.Alert(text, color=color, className="py-2 mb-0 mt-3", dismissable=True)


_TABLE_STYLE = dict(
    style_table={"overflowX": "auto"},
    style_cell={"fontFamily": "Inter, system-ui, sans-serif", "fontSize": "0.8rem",
                "padding": "6px 8px", "textAlign": "left", "minWidth": "110px",
                "maxWidth": "320px", "whiteSpace": "normal", "height": "auto"},
    style_header={"fontWeight": "600", "backgroundColor": "#F3F5F8",
                  "borderBottom": "2px solid #DDE3EA"},
    style_data_conditional=[{"if": {"state": "active"},
                             "backgroundColor": "#E6F4FB", "border": "1px solid #0078C0"}],
)


# ── Layout ─────────────────────────────────────────────────────────────────────

def layout(user: dict) -> html.Div:
    geo = _geo_options()
    return html.Div([
        html.Div([
            html.H4("Settings", className="nhic-page-title"),
            html.Div("Manage who can sign in, who receives reports, when things run, "
                     "and check the health of the system.", className="nhic-page-sub"),
        ], className="mb-3"),
        dbc.Tabs([
            dbc.Tab(_users_tab(geo),      label="Users & roles",     tab_id="users"),
            dbc.Tab(_recipients_tab(),    label="Report recipients", tab_id="recipients"),
            dbc.Tab(_schedules_tab(),     label="Schedules",         tab_id="schedules"),
            dbc.Tab(_status_tab(),        label="System status",     tab_id="status"),
        ], id="set-tabs", active_tab="users", className="nhic-tabs mb-3"),
    ])


def _users_tab(geo: dict) -> html.Div:
    return dbc.Row([
        dbc.Col(_card("Accounts", "bi-people", html.Div(id="set-users-table"),
                      "Everyone who can sign in. Inactive accounts can't sign in but keep "
                      "their history."), lg=7),
        dbc.Col(_card("Add or edit an account", "bi-person-gear", [
            _lbl("Account"),
            dcc.Dropdown(id="set-u-pick", clearable=False, value=_NEW,
                         options=[{"label": "+ New account", "value": _NEW}]),
            dbc.Row([
                dbc.Col([_lbl("Username"), dbc.Input(id="set-u-username", size="sm",
                                                     placeholder="e.g. jdoe")], md=6),
                dbc.Col([_lbl("Full name"), dbc.Input(id="set-u-fullname", size="sm")], md=6),
            ], className="g-2 mt-2"),
            dbc.Row([
                dbc.Col([_lbl("Email"), dbc.Input(id="set-u-email", type="email", size="sm")], md=6),
                dbc.Col([_lbl("Role"), dcc.Dropdown(
                    id="set-u-role", clearable=False, value="health_center",
                    options=[{"label": l, "value": v} for v, l in _ROLES])], md=6),
            ], className="g-2 mt-1"),
            html.Div([_lbl("District"), dcc.Dropdown(id="set-u-district",
                     options=_opts(geo["district"]), placeholder="Select district")],
                     id="set-u-district-wrap", className="mt-2"),
            html.Div([_lbl("Hospital"), dcc.Dropdown(id="set-u-hospital",
                     options=_opts(geo["district_hospital"]),
                     placeholder="Select hospital (pick a district first to narrow the list)")],
                     id="set-u-hospital-wrap", className="mt-2"),
            html.Div([_lbl("Health centre"), dcc.Dropdown(id="set-u-facility",
                     options=_opts(geo["health_facility"]),
                     placeholder="Select health centre (only the chosen hospital's catchment)")],
                     id="set-u-facility-wrap", className="mt-2"),
            html.Div(id="set-u-catchment", className="mt-2"),
            dbc.Row([
                dbc.Col([_lbl("Password"),
                         dbc.Input(id="set-u-password", type="password", size="sm",
                                   autoComplete="new-password",
                                   placeholder="At least 8 characters")], md=8),
                dbc.Col([_lbl("Active"), dbc.Switch(id="set-u-active", value=True,
                                                    className="mt-1")], md=4),
            ], className="g-2 mt-2"),
            html.Div(id="set-u-pw-hint", className="nhic-hint mt-1"),
            dbc.Button([html.I(className="bi bi-check2 me-1"), "Save account"],
                       id="set-u-save", className="btn-nhic mt-3", size="sm"),
            html.Div(id="set-u-msg"),
        ], "Users only see data for their own district, hospital or health centre. "
           "Ministry users see everything and can open this Settings page."), lg=5),
    ], className="g-3")


def _recipients_tab() -> html.Div:
    hosp_cols = [
        ("hospital_name", "Hospital"), ("district", "District"),
        ("hospital_email", "Hospital email"), ("hospital_contact_person", "Contact person"),
        ("nutrition_coordinator_name", "Nutrition coordinator"),
        ("nutrition_coordinator_email", "Coordinator email"),
        ("dho", "District health officers  (Name <email>; …)"),
        ("phone", "Phone"), ("active", "Active"),
    ]
    nat_cols = [("name", "Name"), ("email", "Email"), ("organization", "Organization"),
                ("title", "Title"), ("active", "Active")]

    def _table(tid, cols):
        return dash_table.DataTable(
            id=tid, editable=True, row_deletable=True, page_size=15,
            sort_action="native", filter_action="native",
            columns=[{"id": c, "name": n, **({"presentation": "dropdown"} if c == "active" else {})}
                     for c, n in cols],
            dropdown={"active": {"options": [{"label": "Yes", "value": "Yes"},
                                             {"label": "No", "value": "No"}]}},
            **_TABLE_STYLE)

    return html.Div([
        _card("Hospital report contacts", "bi-hospital", [
            _table("set-hosp-table", hosp_cols),
            html.Div([
                dbc.Button([html.I(className="bi bi-plus-lg me-1"), "Add hospital"],
                           id="set-hosp-add", color="light", size="sm", className="me-2"),
                dbc.Button([html.I(className="bi bi-check2 me-1"), "Save hospital contacts"],
                           id="set-hosp-save", className="btn-nhic", size="sm"),
            ], className="mt-3"),
            html.Div(id="set-hosp-msg"),
        ], "Each hospital's monthly report is emailed to its hospital email. The hospital "
           "name must match the name in eTracker data. Click a cell to edit; use × to "
           "remove a row."),
        _card("National overview recipients", "bi-building", [
            _table("set-nat-table", nat_cols),
            html.Div([
                dbc.Button([html.I(className="bi bi-plus-lg me-1"), "Add recipient"],
                           id="set-nat-add", color="light", size="sm", className="me-2"),
                dbc.Button([html.I(className="bi bi-check2 me-1"), "Save recipients"],
                           id="set-nat-save", className="btn-nhic", size="sm"),
            ], className="mt-3"),
            html.Div(id="set-nat-msg"),
        ], "Senior officials who receive the national overview PDF when it's included "
           "in a report run."),
    ])


def _schedules_tab() -> html.Div:
    import jobs
    sched = jobs.load_report_schedule()
    try:
        hours = float(os.environ.get("AUTO_SYNC_HOURS", "0") or 0)
    except ValueError:
        hours = 0
    sync_text = (f"Every {hours:g} hour(s), counted from the last app restart."
                 if hours > 0 else "Off — data arrives only when someone runs a sync.")
    return html.Div([
        _card("Monthly report auto-send", "bi-calendar-check", [
            dbc.Row([
                dbc.Col([_lbl("Enabled"), dbc.Switch(id="set-sched-enabled",
                         value=bool(sched["enabled"]), className="mt-1")], xs=6, md=2),
                dbc.Col([_lbl("Send on day"), dcc.Dropdown(
                    id="set-sched-day", clearable=False, value=int(sched["day"]),
                    options=[{"label": f"Day {d} of the month", "value": d}
                             for d in range(1, 29)])], xs=6, md=3),
                dbc.Col([_lbl("Level"), dbc.RadioItems(
                    id="set-sched-level", inline=True, value=sched["level"], className="mt-1",
                    options=[{"label": " Hospital", "value": "hospital"},
                             {"label": " Facility", "value": "facility"}])], xs=8, md=3),
                dbc.Col([_lbl("Test mode"), dbc.Switch(id="set-sched-dry",
                         value=bool(sched["dry_run"]), className="mt-1")], xs=4, md=2),
                dbc.Col(dbc.Button([html.I(className="bi bi-check2 me-1"), "Save"],
                                   id="set-sched-save", className="btn-nhic mt-3", size="sm"),
                        xs=12, md=2),
            ], className="g-2 align-items-end"),
            html.Div(id="set-sched-msg"),
        ], "On the chosen day, the previous month's reports are generated and emailed. "
           "Test mode sends everything to the test inbox instead of real contacts."),
        _card("eTracker sync", "bi-arrow-repeat", html.Dl([
            html.Dt("Automatic sync"), html.Dd(sync_text),
            html.Dt("How it works"), html.Dd("Each district fetches only records changed in "
                                            "eTracker since its last successful sync; the "
                                            "dashboard then updates just the affected children."),
            html.Dt("Run now"), html.Dd(dcc.Link("At-Risk Children → Sync eTracker",
                                                 href="/rsss_app/risk")),
        ], className="nhic-kv"),
            "A nightly schedule with a chosen time is the next planned improvement."),
    ])


def _status_tab() -> html.Div:
    return html.Div([
        html.Div([
            dbc.Button([html.I(className="bi bi-arrow-clockwise me-1"), "Refresh"],
                       id="set-status-refresh", color="light", size="sm", className="me-2"),
            dbc.Button([html.I(className="bi bi-envelope-check me-1"), "Test email connection"],
                       id="set-email-test", color="light", size="sm"),
            html.Span("Connects to the mail server and signs in — sends nothing.",
                      className="nhic-hint ms-2"),
        ], className="mb-3"),
        html.Div(id="set-email-msg", className="mb-3"),
        dcc.Loading(html.Div(id="set-status-body"), type="dot", color="#0078C0"),
    ])


# ── Status data ───────────────────────────────────────────────────────────────

def _status_content() -> html.Div:
    import data
    from config.db_local import get_local_conn

    meta = data._read_meta()
    built = (datetime.fromtimestamp(meta["built_at"]).strftime("%d %b %Y %H:%M")
             if meta.get("built_at") else "never")
    as_of = meta.get("data_as_of")
    as_of = datetime.fromisoformat(as_of).strftime("%d %b %Y %H:%M") if as_of else "—"
    cache_card = _card("Dashboard data", "bi-lightning-charge", html.Dl([
        html.Dt("Children"), html.Dd(f"{meta.get('n_children', 0):,}"),
        html.Dt("Newest eTracker data"), html.Dd(as_of),
        html.Dt("Last checked / rebuilt"), html.Dd(built),
        html.Dt("Status"), html.Dd(data.child_build_status()),
    ], className="nhic-kv"))

    rows, db_dl = [], None
    try:
        conn = get_local_conn(); cur = conn.cursor()
        cur.execute("SELECT pg_size_pretty(pg_database_size(current_database())), "
                    "(SELECT COUNT(*) FROM immunization_vaccination)")
        size, total = cur.fetchone()
        cur.execute("SELECT to_regclass('sync_state') IS NOT NULL")
        has_state = cur.fetchone()[0]
        cur.execute(
            "SELECT v.district_source, COUNT(*), MAX(v.last_updated_on), MAX(v.fetched_at)"
            + (", s.synced_through" if has_state else ", NULL")
            + " FROM immunization_vaccination v"
            + (" LEFT JOIN sync_state s ON s.district = v.district_source" if has_state else "")
            + " WHERE v.district_source IS NOT NULL GROUP BY v.district_source"
            + (", s.synced_through" if has_state else "") + " ORDER BY 1")
        rows = cur.fetchall()
        cur.close(); conn.close()
        db_dl = html.Dl([html.Dt("Connection"), html.Dd("OK", className="nhic-status-ok"),
                         html.Dt("Visit records"), html.Dd(f"{total:,}"),
                         html.Dt("Database size"), html.Dd(size)], className="nhic-kv")
    except Exception as exc:
        db_dl = html.Dl([html.Dt("Connection"),
                         html.Dd(f"Failed: {exc}", className="nhic-status-bad")],
                        className="nhic-kv")
    db_card = _card("Database", "bi-database", db_dl)

    now = datetime.now()
    body = []
    for district, n, last_upd, fetched, synced in rows:
        ref = fetched or None
        age = (now - ref).days if ref else None
        cls, txt = (("nhic-status-ok", "Up to date") if age is not None and age <= 2 else
                    ("nhic-status-warn", f"{age} days old") if age is not None and age <= 7 else
                    ("nhic-status-bad", f"{age} days old" if age is not None else "Never"))
        body.append(html.Tr([
            html.Td(district), html.Td(f"{n:,}", className="text-end"),
            html.Td((last_upd or "—")[:16]),
            html.Td(fetched.strftime("%d %b %Y %H:%M") if fetched else "—"),
            html.Td(synced.strftime("%d %b %Y") if synced else "—"),
            html.Td(txt, className=cls),
        ]))
    district_card = _card("Data freshness by district", "bi-geo-alt", html.Div(dbc.Table([
        html.Thead(html.Tr([html.Th("District"), html.Th("Records", className="text-end"),
                            html.Th("Newest change in eTracker"), html.Th("Last fetched"),
                            html.Th("Synced through"), html.Th("Status")])),
        html.Tbody(body),
    ], size="sm", hover=True, className="mb-0"), className="nhic-table-wrap"),
        "'Synced through' is the bookmark the next incremental sync starts from. "
        "Status is based on when data was last fetched.")

    return html.Div([dbc.Row([dbc.Col(cache_card, md=6), dbc.Col(db_card, md=6)], className="g-3"),
                     district_card])


# ── Recipients <-> table rows ──────────────────────────────────────────────────

def _dho_to_text(lst) -> str:
    out = []
    for d in lst or []:
        email = (d.get("email") or "").strip()
        name = (d.get("name") or "").strip()
        if email:
            out.append(f"{name} <{email}>" if name else email)
    return "; ".join(out)


def _text_to_dho(text: str) -> tuple[list[dict], list[str]]:
    """'Name <email>; email2' → ([{name, email, role}, …], [unparseable parts])."""
    out, bad = [], []
    for part in (text or "").split(";"):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(.*?)\s*<\s*([^>]+?)\s*>$", part)
        name, email = (m.group(1).strip(), m.group(2)) if m else ("", part)
        if _EMAIL_RE.match(email):
            out.append({"name": name, "email": email, "role": "District Health Officer"})
        else:
            bad.append(part)
    return out, bad


def _hospital_rows() -> list[dict]:
    raw = _read_json(HOSPITALS_JSON, {"hospitals": []})
    return [{
        "hospital_name": h.get("hospital_name", ""), "district": h.get("district", ""),
        "hospital_email": h.get("hospital_email", ""),
        "hospital_contact_person": h.get("hospital_contact_person", ""),
        "nutrition_coordinator_name": h.get("nutrition_coordinator_name", ""),
        "nutrition_coordinator_email": h.get("nutrition_coordinator_email", ""),
        "dho": _dho_to_text(h.get("district_health_officers")),
        "phone": h.get("phone", ""), "active": "Yes" if h.get("active", True) else "No",
    } for h in raw.get("hospitals", [])]


def _national_rows() -> list[dict]:
    raw = _read_json(NATIONAL_JSON, {"recipients": []})
    return [{**{k: r.get(k, "") for k in ("name", "email", "organization", "title")},
             "active": "Yes" if r.get("active", True) else "No"}
            for r in raw.get("recipients", [])]


# ── Users table ────────────────────────────────────────────────────────────────

def _users_table(users: list[dict]) -> html.Div:
    from auth import role_label
    rows = []
    for u in users:
        role = u.get("role")
        scope = (u.get("health_center") if role == "health_center" else
                 u.get("hospital") if role == "hospital" else
                 u.get("district") if role == "district" else None) or "All of Rwanda"
        rows.append(html.Tr([
            html.Td([html.Div(u.get("full_name") or u["username"], className="fw-semibold"),
                     html.Div(u["username"], className="nhic-hint")]),
            html.Td(role_label(u.get("role", ""))),
            html.Td(scope),
            html.Td(dbc.Badge("Active" if u.get("active") else "Inactive",
                              color="success" if u.get("active") else "secondary")),
        ]))
    return html.Div(dbc.Table([
        html.Thead(html.Tr([html.Th("Name"), html.Th("Role"), html.Th("Scope"), html.Th("Status")])),
        html.Tbody(rows),
    ], size="sm", hover=True, className="mb-0"), className="nhic-table-wrap")


def _user_pick_options(users: list[dict]) -> list[dict]:
    return [{"label": "+ New account", "value": _NEW}] + [
        {"label": f"{u.get('full_name') or u['username']} ({u['username']})", "value": u["username"]}
        for u in users]


# ── Callbacks ──────────────────────────────────────────────────────────────────

def register_callbacks(app) -> None:

    # Users: list + account picker (initial load and after every save)
    @app.callback(Output("set-users-table", "children"),
                  Output("set-u-pick", "options"),
                  Input("set-tabs", "active_tab"),
                  Input("set-u-msg", "children"))
    def _load_users(_tab, _msg_):
        if not _is_admin():
            return "Not authorised.", no_update
        from utils.auth_models import UserDatabase
        users = UserDatabase().get_all_users()
        return _users_table(users), _user_pick_options(users)

    # Users: fill the form from the picked account
    @app.callback(Output("set-u-username", "value"), Output("set-u-username", "disabled"),
                  Output("set-u-fullname", "value"), Output("set-u-email", "value"),
                  Output("set-u-role", "value"), Output("set-u-district", "value"),
                  Output("set-u-hospital", "value"), Output("set-u-facility", "value"),
                  Output("set-u-active", "value"), Output("set-u-password", "value"),
                  Output("set-u-pw-hint", "children"),
                  Input("set-u-pick", "value"))
    def _fill_user(pick):
        if not _is_admin():
            return (no_update,) * 11
        if not pick or pick == _NEW:
            return "", False, "", "", "health_center", None, None, None, True, "", \
                   "Required for new accounts."
        from utils.auth_models import UserDatabase
        u = UserDatabase().get_user(pick) or {}
        geo = _geo_options()
        fac  = u.get("health_center")
        hosp = u.get("hospital") or geo["fac_hospital"].get(fac)
        dist = u.get("district") or geo["hosp_district"].get(hosp)
        return (u.get("username", pick), True, u.get("full_name") or "", u.get("email") or "",
                u.get("role") or "health_center", dist, hosp, fac, bool(u.get("active")), "",
                "Leave blank to keep the current password, or type a new one to reset it.")

    # Users: show only the scope field the role needs
    @app.callback(Output("set-u-district-wrap", "style"), Output("set-u-hospital-wrap", "style"),
                  Output("set-u-facility-wrap", "style"), Input("set-u-role", "value"))
    def _scope_fields(role):
        # Higher levels are shown as filters for the lower ones: a health-centre
        # account picks district → hospital → health centre.
        show, hide = {"display": "block"}, {"display": "none"}
        return (show if role in ("district", "hospital", "health_center") else hide,
                show if role in ("hospital", "health_center") else hide,
                show if role == "health_center" else hide)

    # Users: each list narrows to the level above it (catchment)
    @app.callback(Output("set-u-hospital", "options"), Output("set-u-facility", "options"),
                  Input("set-u-district", "value"), Input("set-u-hospital", "value"),
                  State("set-u-facility", "value"))
    def _narrow(district, hospital, facility):
        geo = _geo_options()
        hosps = _hospitals_in(geo, district)
        facs  = _facilities_in(geo, district, hospital if hospital in hosps else None)
        # keep a saved value visible even if the data has moved it
        if hospital and hospital not in hosps:
            hosps = [hospital] + hosps
        if facility and facility not in facs:
            facs = [facility] + facs
        return _opts(hosps), _opts(facs)

    # Users: what will this account see?
    @app.callback(Output("set-u-catchment", "children"),
                  Input("set-u-role", "value"), Input("set-u-district", "value"),
                  Input("set-u-hospital", "value"), Input("set-u-facility", "value"))
    def _catchment(role, district, hospital, facility):
        if not _is_admin():
            return no_update
        geo = _geo_options()
        if role == "ministry":
            text = "This account will see all of Rwanda and can open Settings."
        elif role == "district" and district:
            h = _hospitals_in(geo, district)
            text = (f"This account will see {district}: {len(h)} hospital(s), "
                    f"{len(_facilities_in(geo, district, None))} health centre(s).")
        elif role == "hospital" and hospital:
            text = (f"This account will see {hospital}'s catchment: "
                    f"{len(_facilities_in(geo, None, hospital))} health centre(s).")
        elif role == "health_center" and facility:
            text = f"This account will see {facility} only."
        else:
            return None
        return html.Div([html.I(className="bi bi-eye me-1"), text], className="nhic-hint")

    # Users: save
    @app.callback(Output("set-u-msg", "children"),
                  Input("set-u-save", "n_clicks"),
                  State("set-u-pick", "value"), State("set-u-username", "value"),
                  State("set-u-fullname", "value"), State("set-u-email", "value"),
                  State("set-u-role", "value"), State("set-u-district", "value"),
                  State("set-u-hospital", "value"), State("set-u-facility", "value"),
                  State("set-u-password", "value"), State("set-u-active", "value"),
                  prevent_initial_call=True)
    def _save_user(_n, pick, username, full_name, email, role, district, hospital,
                   facility, password, active):
        if not _is_admin():
            return _msg("Not authorised.", "danger")
        from utils.auth_models import UserDatabase
        db = UserDatabase()
        is_new = not pick or pick == _NEW
        username = (username or "").strip().lower()
        password = password or ""
        email = (email or "").strip()

        if is_new and not _USERNAME_RE.match(username):
            return _msg("Username: 3–32 characters, lowercase letters, digits, . _ - only.", "warning")
        if is_new and db.get_user(username):
            return _msg(f"Username '{username}' is already taken.", "warning")
        if (is_new or password) and len(password) < 8:
            return _msg("Password must be at least 8 characters.", "warning")
        if email and not _EMAIL_RE.match(email):
            return _msg("That email address doesn't look valid.", "warning")
        geo = _geo_options()
        # Store the account's own level plus its parents (for display/editing);
        # access is decided by the role's own level only (data.filter_by_user).
        if role == "health_center" and facility:
            hospital = hospital or geo["fac_hospital"].get(facility)
        if role in ("hospital", "health_center") and hospital:
            district = district or geo["hosp_district"].get(hospital)
        scope = {"district": district if role in ("district", "hospital", "health_center") else None,
                 "hospital": hospital if role in ("hospital", "health_center") else None,
                 "health_center": facility if role == "health_center" else None}
        need = {"district": "district", "hospital": "hospital", "health_center": "health_center"}.get(role)
        if need and not scope[need]:
            return _msg("Choose the district, hospital or health centre this account covers.", "warning")
        if role == "health_center" and geo["fac_hospital"] and \
                geo["fac_hospital"].get(facility) not in (None, hospital):
            return _msg(f"{facility} is in {geo['fac_hospital'][facility]}'s catchment, "
                        f"not {hospital}'s.", "warning")
        if role in ("hospital", "health_center") and geo["hosp_district"] and \
                geo["hosp_district"].get(hospital) not in (None, district):
            return _msg(f"{hospital} is in {geo['hosp_district'][hospital]}, not {district}.", "warning")

        me = (_user() or {}).get("username")
        if not is_new and pick == me and (role != "ministry" or not active):
            return _msg("You can't remove your own admin access or deactivate yourself — "
                        "ask another ministry admin.", "warning")

        fields = dict(full_name=(full_name or "").strip() or username, role=role,
                      email=email, active=int(bool(active)), **scope)
        if is_new:
            db.create_user(username, password, full_name=fields["full_name"], role=role,
                           email=email, **scope)
            if not active:
                db.set_active(username, False)
            return _msg(f"Account '{username}' created.")
        if password:
            fields["password"] = password
        db.update_user(pick, **fields)
        return _msg(f"Account '{pick}' updated" + (" (password reset)." if password else "."))

    # Recipients: load tables
    @app.callback(Output("set-hosp-table", "data"), Output("set-nat-table", "data"),
                  Input("set-tabs", "active_tab"))
    def _load_recipients(tab):
        if tab != "recipients" or not _is_admin():
            return no_update, no_update
        return _hospital_rows(), _national_rows()

    @app.callback(Output("set-hosp-table", "data", allow_duplicate=True),
                  Input("set-hosp-add", "n_clicks"), State("set-hosp-table", "data"),
                  prevent_initial_call=True)
    def _add_hosp(_n, rows):
        return (rows or []) + [{"hospital_name": "", "district": "", "hospital_email": "",
                                "active": "Yes"}]

    @app.callback(Output("set-nat-table", "data", allow_duplicate=True),
                  Input("set-nat-add", "n_clicks"), State("set-nat-table", "data"),
                  prevent_initial_call=True)
    def _add_nat(_n, rows):
        return (rows or []) + [{"name": "", "email": "", "active": "Yes"}]

    @app.callback(Output("set-hosp-msg", "children"),
                  Input("set-hosp-save", "n_clicks"), State("set-hosp-table", "data"),
                  prevent_initial_call=True)
    def _save_hosp(_n, rows):
        if not _is_admin():
            return _msg("Not authorised.", "danger")
        problems, hospitals, seen = [], [], set()
        for i, r in enumerate(rows or [], start=1):
            name = (r.get("hospital_name") or "").strip()
            email = (r.get("hospital_email") or "").strip()
            if not name and not email:
                continue                                   # blank row — ignore
            if not name:
                problems.append(f"Row {i}: hospital name is empty."); continue
            if name.lower() in seen:
                problems.append(f"'{name}' appears twice."); continue
            seen.add(name.lower())
            for label, val in (("hospital email", email),
                               ("coordinator email", (r.get("nutrition_coordinator_email") or "").strip())):
                if val and not _EMAIL_RE.match(val):
                    problems.append(f"{name}: {label} '{val}' isn't valid.")
            if not email:
                problems.append(f"{name}: hospital email is required.")
            dhos, bad = _text_to_dho(r.get("dho"))
            problems += [f"{name}: district officer '{b}' isn't a valid email." for b in bad]
            hospitals.append({
                "hospital_name": name, "district": (r.get("district") or "").strip(),
                "hospital_email": email,
                "hospital_contact_person": (r.get("hospital_contact_person") or "").strip(),
                "nutrition_coordinator_email": (r.get("nutrition_coordinator_email") or "").strip(),
                "nutrition_coordinator_name": (r.get("nutrition_coordinator_name") or "").strip(),
                "district_health_officers": dhos,
                "phone": (r.get("phone") or "").strip(),
                "active": r.get("active", "Yes") != "No",
            })
        if problems:
            return _msg(html.Div(["Not saved — please fix:",
                                  html.Ul([html.Li(p) for p in problems[:12]], className="mb-0")]),
                        "warning")
        raw = _read_json(HOSPITALS_JSON, {})
        meta = {**raw.get("metadata", {}),
                "last_updated": datetime.now().strftime("%Y-%m-%d"),
                "updated_by": (_user() or {}).get("full_name") or (_user() or {}).get("username"),
                "total_hospitals": len(hospitals)}
        try:
            _write_json(HOSPITALS_JSON, {"hospitals": hospitals, "metadata": meta})
        except OSError as exc:
            return _msg(f"Could not save: {exc}", "danger")

        # Names that won't match eTracker data get no report — warn, don't block.
        from jobs import _norm_name
        known = {_norm_name(n) for n in _geo_options()["district_hospital"]}
        unmatched = [h["hospital_name"] for h in hospitals
                     if known and _norm_name(h["hospital_name"]) not in known]
        text = f"Saved {len(hospitals)} hospital contacts."
        if unmatched:
            return _msg(f"{text} These names don't match any hospital in the data, so they "
                        f"won't receive reports: {', '.join(unmatched[:10])}", "warning")
        return _msg(text)

    @app.callback(Output("set-nat-msg", "children"),
                  Input("set-nat-save", "n_clicks"), State("set-nat-table", "data"),
                  prevent_initial_call=True)
    def _save_nat(_n, rows):
        if not _is_admin():
            return _msg("Not authorised.", "danger")
        problems, recips = [], []
        for i, r in enumerate(rows or [], start=1):
            email = (r.get("email") or "").strip()
            name = (r.get("name") or "").strip()
            if not email and not name:
                continue
            if not _EMAIL_RE.match(email):
                problems.append(f"Row {i} ({name or 'no name'}): email '{email}' isn't valid.")
                continue
            recips.append({"name": name, "email": email,
                           "organization": (r.get("organization") or "").strip(),
                           "title": (r.get("title") or "").strip(),
                           "active": r.get("active", "Yes") != "No"})
        if problems:
            return _msg(html.Div(["Not saved — please fix:",
                                  html.Ul([html.Li(p) for p in problems], className="mb-0")]),
                        "warning")
        raw = _read_json(NATIONAL_JSON, {})
        try:
            _write_json(NATIONAL_JSON, {"recipients": recips,
                                        "metadata": {**raw.get("metadata", {}),
                                                     "last_updated": datetime.now().strftime("%Y-%m-%d")}})
        except OSError as exc:
            return _msg(f"Could not save: {exc}", "danger")
        return _msg(f"Saved {len(recips)} national recipients.")

    # Schedules: monthly report auto-send
    @app.callback(Output("set-sched-msg", "children"),
                  Input("set-sched-save", "n_clicks"),
                  State("set-sched-enabled", "value"), State("set-sched-day", "value"),
                  State("set-sched-level", "value"), State("set-sched-dry", "value"),
                  prevent_initial_call=True)
    def _save_schedule(_n, enabled, day, level, dry):
        if not _is_admin():
            return _msg("Not authorised.", "danger")
        import jobs
        try:
            jobs.save_report_schedule({"enabled": bool(enabled), "day": int(day or 1),
                                       "level": level or "hospital", "dry_run": bool(dry)})
        except OSError as exc:
            return _msg(f"Could not save: {exc}", "danger")
        if not enabled:
            return _msg("Saved. Automatic sending is off.")
        who = "the test inbox only" if dry else "real contacts"
        return _msg(f"Saved. {level.title()} reports for the previous month will be emailed "
                    f"to {who} on day {day} of each month.")

    # System status
    @app.callback(Output("set-status-body", "children"),
                  Input("set-tabs", "active_tab"), Input("set-status-refresh", "n_clicks"))
    def _status(tab, _n):
        if tab != "status" or not _is_admin():
            return no_update
        return _status_content()

    @app.callback(Output("set-email-msg", "children"),
                  Input("set-email-test", "n_clicks"), prevent_initial_call=True)
    def _email_test(_n):
        if not _is_admin():
            return _msg("Not authorised.", "danger")
        from config.email_config import email_config as cfg
        from core.email_sender import EmailSender
        sender = EmailSender()
        ok = sender.connect()
        sender.disconnect()
        where = f"{cfg.SMTP_HOST}:{cfg.SMTP_PORT}"
        if ok:
            return _msg(f"Connected and signed in to {where}.")
        return _msg(f"Could not connect or sign in to {where}. Check the email settings in "
                    f".env and that the server can reach the mail server.", "danger")
