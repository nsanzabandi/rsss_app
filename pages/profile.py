"""
pages/profile.py — "My profile" for every signed-in user (not view-only links).

Users can change their own display name and password. Email, role and area
are shown read-only: only a ministry administrator can change those or create
accounts (pages/settings.py).
"""
from __future__ import annotations

import dash_bootstrap_components as dbc
from dash import Input, Output, State, html, no_update

_MIN_PW = 8


def _me() -> dict | None:
    from flask import session
    u = session.get("user")
    return None if not u or u.get("readonly") else u


def _msg(text: str, color: str = "success") -> dbc.Alert:
    return dbc.Alert(text, color=color, className="py-2 mb-0 mt-3", dismissable=True)


def _card(title: str, icon: str, body, hint: str | None = None) -> dbc.Card:
    head = [html.Div([html.I(className=f"bi {icon}"), title], className="nhic-card-title")]
    if hint:
        head.append(html.Div(hint, className="nhic-hint mt-1"))
    return dbc.Card(dbc.CardBody(head + [html.Div(body, className="mt-3")]), className="nhic-card mb-3")


def _lbl(text: str) -> dbc.Label:
    return dbc.Label(text, className="nhic-hint fw-semibold mb-1")


def layout(user: dict) -> html.Div:
    from auth import role_label
    from utils.auth_models import UserDatabase
    rec = UserDatabase().get_user(user.get("username", "")) or {}
    role = rec.get("role") or user.get("role", "")
    area = (rec.get("health_center") if role == "health_center" else
            rec.get("hospital") if role == "hospital" else
            rec.get("district") if role == "district" else None) or "All of Rwanda"

    return html.Div([
        html.Div([html.H4("My profile", className="nhic-page-title"),
                  html.Div("Your account details and password.", className="nhic-page-sub")],
                 className="mb-3"),
        dbc.Row([
            dbc.Col(_card("Account", "bi-person-badge", [
                html.Dl([
                    html.Dt("Username"), html.Dd(rec.get("username") or user.get("username")),
                    html.Dt("Email"),    html.Dd(rec.get("email") or "—"),
                    html.Dt("Role"),     html.Dd(role_label(role)),
                    html.Dt("Area"),     html.Dd(area),
                ], className="nhic-kv"),
                html.Div("To change your email, role or area, contact an NHIC administrator.",
                         className="nhic-hint mt-3"),
                html.Hr(),
                _lbl("Display name"),
                dbc.Input(id="pf-name", value=rec.get("full_name") or "", size="sm", maxLength=80),
                dbc.Button([html.I(className="bi bi-check2 me-1"), "Save name"],
                           id="pf-name-save", className="btn-nhic mt-3", size="sm"),
                html.Div(id="pf-name-msg"),
            ]), lg=6),
            dbc.Col(_card("Change password", "bi-key", [
                _lbl("Current password"),
                dbc.Input(id="pf-cur", type="password", size="sm", autoComplete="current-password"),
                _lbl("New password"),
                dbc.Input(id="pf-new", type="password", size="sm", autoComplete="new-password",
                          className="mb-0"),
                html.Div(f"At least {_MIN_PW} characters.", className="nhic-hint mb-2"),
                _lbl("Repeat new password"),
                dbc.Input(id="pf-new2", type="password", size="sm", autoComplete="new-password"),
                dbc.Button([html.I(className="bi bi-shield-lock me-1"), "Change password"],
                           id="pf-pw-save", className="btn-nhic mt-3", size="sm"),
                html.Div(id="pf-pw-msg"),
            ], "You'll stay signed in on this device after changing it."), lg=6),
        ], className="g-3"),
    ])


def register_callbacks(app) -> None:

    @app.callback(Output("pf-name-msg", "children"),
                  Input("pf-name-save", "n_clicks"), State("pf-name", "value"),
                  prevent_initial_call=True)
    def _save_name(_n, name):
        me = _me()
        if not me:
            return _msg("Not available for this account.", "danger")
        name = (name or "").strip()
        if len(name) < 2:
            return _msg("Please enter your name.", "warning")
        from flask import session
        from utils.auth_models import UserDatabase
        UserDatabase().update_user(me["username"], full_name=name)
        session["user"] = {**me, "full_name": name}   # top bar shows it on next page load
        return _msg("Name saved.")

    @app.callback(Output("pf-pw-msg", "children"),
                  Output("pf-cur", "value"), Output("pf-new", "value"), Output("pf-new2", "value"),
                  Input("pf-pw-save", "n_clicks"),
                  State("pf-cur", "value"), State("pf-new", "value"), State("pf-new2", "value"),
                  prevent_initial_call=True)
    def _change_pw(_n, cur, new, new2):
        me = _me()
        if not me:
            return _msg("Not available for this account.", "danger"), no_update, no_update, no_update
        from auth import authenticate_user
        from utils.auth_models import UserDatabase
        if not authenticate_user(me["username"], cur or ""):
            return _msg("Your current password is not correct.", "warning"), "", no_update, no_update
        if len(new or "") < _MIN_PW:
            return _msg(f"The new password needs at least {_MIN_PW} characters.", "warning"), \
                   no_update, no_update, no_update
        if new != new2:
            return _msg("The two new passwords don't match.", "warning"), no_update, no_update, ""
        if new == cur:
            return _msg("Choose a password different from your current one.", "warning"), \
                   no_update, no_update, no_update
        UserDatabase().update_user(me["username"], password=new)
        return _msg("Password changed."), "", "", ""
