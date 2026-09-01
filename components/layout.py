"""
components/layout.py — Sidebar shell + login form.

serve_layout() is assigned to app.layout (as a function, not a value)
so it is called fresh on every page load, reading flask.session each time.
"""
from pathlib import Path

from dash import html, dcc
import dash_bootstrap_components as dbc

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"

SIDEBAR_BG    = "#3B4148"   # neutral gray sidebar
SIDEBAR_TEXT  = "#CBD3DC"
SIDEBAR_LINE  = "#525A63"   # divider colour on the gray sidebar
PRIMARY       = "#C0392B"
SIDEBAR_W     = "220px"


def _nav_link(label: str, href: str, icon: str) -> dbc.NavLink:
    return dbc.NavLink(
        [html.I(className=f"bi {icon} me-2"), label],
        href=href,
        active="exact",
        style={
            "color": SIDEBAR_TEXT,
            "borderRadius": "6px",
            "padding": "8px 12px",
            "marginBottom": "2px",
            "fontSize": "0.87rem",
            "fontWeight": "500",
        },
        className="sidebar-link",
    )


def _rbc_logo() -> html.Div:
    """RBC logo mark. Uses the official image if you save it to
    assets/rbc_logo.png; otherwise shows a clean built-in badge."""
    if (_ASSETS_DIR / "rbc_logo.png").exists():
        return html.Img(src="/assets/rbc_logo.png",
                        style={"height": "42px", "background": "#fff",
                               "borderRadius": "6px", "padding": "3px"})
    return html.Div("RBC", style={
        "background": "#FFFFFF", "color": PRIMARY, "fontWeight": "800",
        "fontSize": "0.95rem", "borderRadius": "8px", "padding": "8px 9px",
        "letterSpacing": "0.02em", "lineHeight": "1",
        "boxShadow": "0 1px 3px rgba(0,0,0,.25)"})


def _sidebar(user: dict | None = None) -> html.Div:
    role     = (user or {}).get("role", "")
    readonly = bool((user or {}).get("readonly"))

    nav_items: list = [
        _nav_link("Dashboard", "/rsss_app/",         "bi-speedometer2"),
    ]
    if not readonly:   # operational page — hidden for view-only link users
        nav_items.append(_nav_link("Reports", "/rsss_app/reports", "bi-file-earmark-pdf"))
    nav_items += [
        _nav_link("Follow-Up", "/rsss_app/followup", "bi-clipboard2-pulse"),
        _nav_link("At-Risk",   "/rsss_app/risk",     "bi-exclamation-triangle-fill"),
        _nav_link("eBuzima Nutrition", "/rsss_app/ebuzima", "bi-clipboard2-data"),
    ]

    if role == "ministry" and not readonly:
        nav_items += [
            html.Hr(style={"borderColor": SIDEBAR_LINE, "margin": "8px 0"}),
            _nav_link("Settings", "/rsss_app/settings", "bi-gear"),
        ]

    footer_text = ""
    if user:
        from auth import role_label
        footer_text = (
            f"{user.get('full_name', user.get('username', ''))} "
            f"· {role_label(role)}"
        )

    return html.Div(
        [
            html.Div([
                html.Div([
                    _rbc_logo(),
                    html.Div([
                        html.Div("RSSS", style={
                            "color": "#FFFFFF", "fontWeight": "800",
                            "fontSize": "1.05rem", "letterSpacing": "0.05em",
                            "lineHeight": "1.05"}),
                        html.Div("Rwanda Stunting Surveillance",
                                 style={"color": SIDEBAR_TEXT, "fontSize": "0.6rem",
                                        "letterSpacing": "0.03em"}),
                    ]),
                ], className="d-flex align-items-center", style={"gap": "10px"}),
            ], style={"padding": "18px 14px 14px"}),

            html.Hr(style={"borderColor": SIDEBAR_LINE, "margin": "0 0 8px"}),

            dbc.Nav(nav_items, vertical=True, pills=True,
                    style={"padding": "0 8px", "flexGrow": "1"}),

            html.Div([
                html.Hr(style={"borderColor": SIDEBAR_LINE, "margin": "0 0 8px"}),
                html.Div(footer_text,
                         style={"color": SIDEBAR_TEXT, "fontSize": "0.7rem",
                                "lineHeight": "1.3", "marginBottom": "8px"}),
                html.A(
                    [html.I(className="bi bi-box-arrow-left me-1"), "Logout"],
                    href="/rsss_app/logout",
                    style={"color": "#E74C3C", "fontSize": "0.78rem",
                           "textDecoration": "none"},
                ),
            ], style={"padding": "8px 14px 16px"}),
        ],
        id="sidebar",
        style={
            "width": SIDEBAR_W, "minHeight": "100vh",
            "background": SIDEBAR_BG, "display": "flex",
            "flexDirection": "column", "position": "fixed",
            "top": 0, "left": 0, "zIndex": 100,
            "boxShadow": "2px 0 8px rgba(0,0,0,.15)",
        },
    )


def _app_shell(user: dict) -> html.Div:
    return html.Div([
        _sidebar(user),
        html.Div(
            html.Div(id="page-content", style={"maxWidth": "1400px"}),
            style={
                "marginLeft": SIDEBAR_W,
                "padding": "24px",
                "minHeight": "100vh",
                "background": "#F0F2F5",
                "flex": "1",
                "minWidth": "0",
                "width": f"calc(100% - {SIDEBAR_W})",
            },
        ),
    ], style={"display": "flex", "width": "100%"})


def login_layout(error: str = "") -> html.Div:
    """Login form — uses a plain HTML POST so Flask handles auth and redirects."""
    return html.Div(
        dbc.Row(
            dbc.Col(
                dbc.Card(
                    dbc.CardBody([
                        html.Div([
                            html.Span("❤", style={"color": PRIMARY, "fontSize": "2rem"}),
                            html.H4("RSSS", className="d-inline ms-2 fw-bold",
                                    style={"color": SIDEBAR_BG}),
                        ], className="text-center mb-1"),
                        html.P("Rwanda Stunting Surveillance System",
                               className="text-center text-muted mb-4",
                               style={"fontSize": "0.8rem"}),

                        html.Div(
                            dbc.Alert(error, color="danger", className="py-2"),
                            style={"display": "block" if error else "none"},
                        ),

                        # Plain HTML form — POSTs to Flask route, triggers real page reload
                        html.Form([
                            dbc.Label("Username",
                                      style={"fontSize": "0.82rem", "fontWeight": "600"}),
                            dcc.Input(
                                name="username", type="text", placeholder="username",
                                autoFocus=True, required=True,
                                className="form-control mb-3",
                                style={"fontSize": "0.9rem", "borderRadius": "6px"},
                            ),
                            dbc.Label("Password",
                                      style={"fontSize": "0.82rem", "fontWeight": "600"}),
                            dcc.Input(
                                name="password", type="password", placeholder="••••••••",
                                required=True,
                                className="form-control mb-4",
                                style={"fontSize": "0.9rem", "borderRadius": "6px"},
                            ),
                            html.Button(
                                "Sign in", type="submit",
                                className="btn btn-danger w-100 fw-semibold",
                            ),
                        ], action="/rsss_app/do-login", method="post"),
                    ]),
                    style={"borderRadius": "12px",
                           "boxShadow": "0 4px 24px rgba(0,0,0,.12)"},
                ),
                xs=12, sm=8, md=5, lg=4, xl=3,
            ),
            justify="center", align="center",
            style={"minHeight": "100vh"},
        ),
        style={"background": "#F0F2F5"},
    )


def serve_layout() -> html.Div:
    """Called by Dash on every page request — reads Flask session."""
    from flask import session as flask_session, request as flask_req
    user = flask_session.get("user")
    if not user:
        try:
            err = "Invalid username or password." if flask_req.args.get("login_error") else ""
        except Exception:
            err = ""
        return html.Div([
            dcc.Location(id="url", refresh=False),
            html.Div(login_layout(err), id="root-container"),
        ])
    return html.Div([
        dcc.Location(id="url", refresh=False),
        html.Div(_app_shell(user), id="root-container"),
    ])
