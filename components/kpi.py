"""
components/kpi.py — KPI card builder for the RSSS dashboard.
"""
from dash import dcc, html
import dash_bootstrap_components as dbc

C_PRIMARY = "#C0392B"
C_SUCCESS = "#27AE60"
C_WARNING = "#E67E22"
C_DANGER  = "#E74C3C"
C_INFO    = "#2980B9"
C_TEAL    = "#1ABC9C"
C_GREY    = "#7F8C8D"


def kpi_card(
    title: str,
    value: str,
    subtitle: str = "",
    color: str = C_PRIMARY,
    icon: str = "",
) -> dbc.Col:
    return dbc.Col(
        dbc.Card(
            dbc.CardBody(
                html.Div([
                    html.Div([
                        html.I(className=f"{icon} me-2",
                               style={"color": color, "fontSize": "1.1rem"})
                        if icon else None,
                        html.Span(title, className="text-muted",
                                  style={"fontSize": "0.72rem", "fontWeight": "600",
                                         "letterSpacing": "0.05em",
                                         "textTransform": "uppercase"}),
                    ], className="d-flex align-items-center mb-1"),
                    html.Div(value,
                             style={"fontSize": "1.65rem", "fontWeight": "700",
                                    "color": "#2C3E50", "lineHeight": "1.1"}),
                    html.Div(subtitle, className="text-muted mt-1",
                             style={"fontSize": "0.72rem"}) if subtitle else None,
                ]),
                className="py-2 px-3",
            ),
            style={
                "borderLeft": f"4px solid {color}",
                "borderRadius": "8px",
                "boxShadow": "0 1px 4px rgba(0,0,0,.07)",
                "background": "#fff",
            },
        ),
        xs=6, md=4, xl=2,
        className="mb-2",
    )


def _tint(hex_color: str) -> str:
    """Return a translucent version of a hex colour for the icon chip."""
    return hex_color + "22"   # ~13% alpha (#RRGGBBAA)


def metric_card(
    label: str,
    value: str,
    sub: str = "",
    color: str = C_PRIMARY,
    icon: str = "",
    href: str | None = None,
    xl=3,
    delta: dict | None = None,
) -> dbc.Col:
    """Modern headline KPI card: soft icon chip + bold value + label.
    href → the whole card is a link (with a hover lift and a 'View' hint)."""
    card = (
        dbc.Card(
            dbc.CardBody(
                html.Div([
                    html.Div(
                        html.I(className=icon,
                               style={"fontSize": "1.3rem", "color": color}) if icon else None,
                        style={
                            "width": "46px", "height": "46px", "borderRadius": "13px",
                            "background": _tint(color), "display": "flex", "flex": "0 0 auto",
                            "alignItems": "center", "justifyContent": "center",
                        },
                    ),
                    html.Div([
                        html.Div(value, style={
                            "fontSize": "1.5rem", "fontWeight": "800", "color": "#1F2A37",
                            "lineHeight": "1.15", "whiteSpace": "nowrap"}),
                        html.Div(label, className="text-muted", style={
                            "fontSize": "0.7rem", "fontWeight": "700",
                            "letterSpacing": "0.05em", "textTransform": "uppercase",
                            "whiteSpace": "nowrap"}),
                        html.Div(sub, className="text-muted",
                                 style={"fontSize": "0.7rem"}) if sub else None,
                        delta_badge(delta) if delta else None,
                    ], style={"minWidth": 0}),
                ], className="d-flex align-items-center", style={"gap": "0.8rem"}),
                className="py-3 px-3",
            ),
            className="border-0 h-100" + (" nhic-card-link" if href else ""),
            style={"borderRadius": "14px",
                   "boxShadow": "0 2px 12px rgba(31,42,55,.07)", "background": "#fff"},
        )
    )
    if href:
        card = dcc.Link(card, href=href, className="text-decoration-none d-block h-100",
                        title="Open the at-risk dashboard for this selection")
    return dbc.Col(card, xs=6, md=4, xl=xl, className="mb-2")


def delta_badge(d: dict) -> html.Div:
    """▲/▼ change vs the previous period. For every card that uses it, a RISE
    is bad (red flag) and a FALL is good (green flag).
    d = {"text": "+3,888 (+9.2%)", "dir": "up"|"down"|"flat",
         "detail": "vs Apr–Jun 2026: 42,405"}"""
    arrow, cls = {"up": ("▲", "nhic-delta-up"), "down": ("▼", "nhic-delta-down")}.get(
        d.get("dir"), ("▬", "nhic-delta-flat"))
    if d.get("text") == "data still arriving":
        arrow = "⏳"
    return html.Div([html.Span([arrow, " ", d["text"]], className=f"nhic-delta {cls}"),
                     html.Div(d.get("detail", ""), className="nhic-delta-vs")],
                    className="mt-1")


def kpi_placeholder() -> dbc.Col:
    return dbc.Col(
        dbc.Card(
            dbc.CardBody(
                html.Div(className="placeholder-glow", children=[
                    html.Span(className="placeholder col-8 mb-1 d-block",
                              style={"height": "10px", "borderRadius": "4px"}),
                    html.Span(className="placeholder col-5 d-block",
                              style={"height": "28px", "borderRadius": "4px"}),
                ]),
                className="py-2 px-3",
            ),
            style={"borderRadius": "14px",
                   "boxShadow": "0 2px 12px rgba(31,42,55,.07)", "background": "#fff"},
        ),
        xs=6, md=4, xl=3,
        className="mb-2",
    )
