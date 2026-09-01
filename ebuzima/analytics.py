"""eBuzima Nutrition Analytics — data loading, classification, and chart builders.

Ported from the standalone ebuzima_nutrition/dashboard.py. This module holds
everything that ISN'T Dash app/layout/callback wiring (that lives in
pages/ebuzima.py) — the equivalent of rsss_app's own core/ + data.py for
this page.

Data is loaded LAZILY (on first ensure_loaded() call from a callback), not at
import time — importing this module must never block the whole app's startup,
matching the same principle pages/risk.py already follows for rsss_app's own
data ("layout() does NOT call get_df() — no blocking calls at render time").
"""
import os
import threading
import warnings

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from .db import get_engine

warnings.filterwarnings("ignore")

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = get_engine()
    return _engine


# ── Theme ─────────────────────────────────────────────────────────────────────
C = dict(
    primary="#1a6faf", success="#2ca25f", warning="#e07b00",
    danger="#d73027", purple="#756bb1", teal="#2b8cbe",
    light="#f5f7fa", border="#dee2e6", text="#2c3e50",
    muted="#6c757d", white="#ffffff",
)
T = "plotly_white"

STUNT_COLORS = {"Severe Stunting": C["danger"], "Moderate Stunting": C["warning"], "Normal": C["success"]}
MAL_COLORS   = {"SAM": C["danger"], "MAM": C["warning"], "Normal": C["success"]}

# ══════════════════════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════════════════════

def _sql_or_csv(table: str) -> pd.DataFrame:
    """Read a table from PostgreSQL or fallback to CSV. Returns an empty DataFrame if both are unavailable."""
    engine = _get_engine()
    if engine is not None:
        try:
            df = pd.read_sql(f'SELECT * FROM "{table}"', engine)
            print(f"  [ebuzima] {table}: {len(df):,} rows (PostgreSQL)")
            return df
        except Exception as e:
            pass

    # Fallback to ClickHouse directly if PostgreSQL fails/missing
    try:
        from . import clickhouse_source
        ch_table = "tab" + table.replace("_", " ").title()
        if ch_table == "tabFathers_Additional_Partner_Children_Detail":
            ch_table = "tabFathers Additional Partner Children Detail"
        elif ch_table == "tabMothers_Additional_Partner_Children_Detail":
            ch_table = "tabMothers Additional Partner Children Detail"
            
        df = clickhouse_source.fetch_all(ch_table)
        if df is not None and not df.empty:
            print(f"  [ebuzima] {table}: {len(df):,} rows (ClickHouse)")
            
            # Mask PII on the fly just like ingest.py does
            try:
                from .db import hash_pii
                df = hash_pii(df)
            except Exception:
                pass
                
            return df
    except Exception as e:
        print(f"  [ebuzima] {table} ClickHouse direct load failed: {e}")

    # Fallback to CSV
    import os
    csv_path = os.path.join(os.path.dirname(__file__), "data", "exports", f"{table}.csv")
    if os.path.exists(csv_path):
        try:
            df = pd.read_csv(csv_path, low_memory=False)
            print(f"  [ebuzima] {table}: {len(df):,} rows (CSV)")
            return df
        except Exception as e:
            print(f"  [ebuzima] {table} CSV load failed: {e}")

    print(f"  [ebuzima] [warn] {table}: no DB or CSV available — returning empty DataFrame")
    return pd.DataFrame()


def _parse_geo(s: pd.Series) -> pd.Series:
    def _c(v):
        if pd.isna(v) or not str(v).strip(): return None
        t = str(v).strip()
        return t.split("#", 1)[1].strip() if "#" in t else t
    return s.apply(_c)


def _age(dob: pd.Series) -> pd.Series:
    d = pd.to_datetime(dob, errors="coerce")
    return ((pd.Timestamp.now() - d).dt.days / 365.25).where(d.notna())


def _bucket(age: pd.Series) -> pd.Series:
    return pd.cut(age,
                  bins=[0, 1, 2, 3, 4, 5, 10, 18, 200],
                  labels=["0–1","1–2","2–3","3–4","4–5","5–10","10–18","18+"],
                  right=False)


def _ts(df, *cols):
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


def _geo(df, cols=("province","district","sector","cell","village")):
    for c in cols:
        if c in df.columns:
            df[c] = _parse_geo(df[c])
    return df


def _num(df, *cols):
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _add_age(df):
    if "date_of_birth" in df.columns:
        df["age_years"]  = _age(df["date_of_birth"])
        df["age_bucket"] = _bucket(df["age_years"])
    return df


def _add_month(df):
    if "creation" in df.columns:
        df["month"] = df["creation"].dt.to_period("M").astype(str)
    return df


def classify_stunting(df: pd.DataFrame) -> pd.DataFrame:
    """Classify stunting from h_a_zscore (preferred) or diagnosis text."""
    df = df.copy()
    # Method 1: numeric HAZ
    if "h_a_zscore" in df.columns:
        haz = pd.to_numeric(df["h_a_zscore"], errors="coerce")
        df["haz"] = haz
        df["stunting_cat"] = np.select(
            [haz <= -3, (haz > -3) & (haz <= -2), haz > -2],
            ["Severe Stunting", "Moderate Stunting", "Normal"],
            default="Unknown",
        )
        df["is_stunted"]     = haz <= -2
        df["is_sev_stunted"] = haz <= -3
    # Method 2: parse diagnosis text if HAZ missing
    elif "diagnosis" in df.columns:
        diag = df["diagnosis"].fillna("").str.lower()
        sev  = diag.str.contains(r"very severe stunt|severe stunt", regex=True)
        mod  = diag.str.contains(r"moderate.*stunt|stunt", regex=True) & ~sev
        norm = ~(sev | mod)
        df["stunting_cat"] = np.select([sev, mod, norm],
                                        ["Severe Stunting","Moderate Stunting","Normal"],
                                        default="Unknown")
        df["is_stunted"]     = sev | mod
        df["is_sev_stunted"] = sev
    return df


def classify_malnutrition(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    muac = pd.to_numeric(df["muac"], errors="coerce")        if "muac"       in df.columns else pd.Series(dtype=float, index=df.index)
    whz  = pd.to_numeric(df["w_h_zscore"], errors="coerce") if "w_h_zscore" in df.columns else pd.Series(dtype=float, index=df.index)
    sam  = (muac < 11.5)  | (whz < -3)
    mam  = (~sam) & ((muac.between(11.5, 12.5)) | (whz.between(-3, -2)))
    df["mal_cat"] = np.select([sam, mam], ["SAM", "MAM"], default="Normal")
    return df


def _load_nutr():
    df = _sql_or_csv("nutrition")
    if df.empty: return df
    df = _geo(df); df = _ts(df, "creation","modified","discharge_date","date_of_entry")
    df = _add_age(df); df = _add_month(df)
    if "discharging" in df.columns:
        df["discharged"] = df["discharging"].astype(str).str.strip().isin(["1","True","true","Yes","yes"])
    if "discharge_date" in df.columns and "date_of_entry" in df.columns:
        days = (df["discharge_date"] - df["date_of_entry"]).dt.days
        df["days_in_program"] = days.where(days >= 0)
    return classify_stunting(df)


def _load_cgm():
    df = _sql_or_csv("child_growth_monitoring")
    if df.empty: return df
    df = _geo(df, ("district","sector","cell","village"))
    df = _ts(df, "creation","modified"); df = _add_age(df); df = _add_month(df)
    _num(df, "birth_weight","birth_height","gestational_age_at_birth")
    if "birth_weight" in df.columns:
        bw = df["birth_weight"]
        df.loc[bw.between(0.5, 6), "birth_weight"] = bw[bw.between(0.5, 6)] * 1000
        df["bw_suspect"] = df["birth_weight"].between(6, 100)
        df["bw_clean"]   = df["birth_weight"].where(df["birth_weight"].between(200, 6000) & ~df["bw_suspect"])
        df["lbw"]        = df["bw_clean"] < 2500
    return df


def _load_ref():
    df = _sql_or_csv("nutrition_referral")
    if df.empty: return df
    df = _ts(df, "creation","referral_date"); df = _add_age(df); df = _add_month(df)
    _num(df, "weight","height","bmi","muac","w_h_zscore","h_a_zscore","w_a_zscore")
    if "company" not in df.columns and "referred_from" in df.columns:
        df["company"] = df["referred_from"]
    df = classify_stunting(df)
    df = classify_malnutrition(df)
    return df


def _load_generic(table: str) -> pd.DataFrame:
    df = _sql_or_csv(table)
    if df.empty: return df
    df = _ts(df, "creation","modified"); df = _add_age(df); df = _add_month(df)
    _num(df, "weight","height","muac","h_a_zscore","w_h_zscore","w_a_zscore")
    df = _geo(df)
    if "h_a_zscore" in df.columns or "diagnosis" in df.columns:
        df = classify_stunting(df)
    if "muac" in df.columns or "w_h_zscore" in df.columns:
        df = classify_malnutrition(df)
    return df


# ── Combined under-5 stunting dataset (from all sources with stunting data) ────

def _build_stunt():
    KEEP = ("name","creation","month","age_years","age_bucket","gender",
            "province","district","sector","company",
            "haz","stunting_cat","is_stunted","is_sev_stunted",
            "muac","w_h_zscore","w_a_zscore","mal_cat","diagnosis")
    frames = []
    for df in (REF, FOLLOWUP, CGM_FU, NUTR):
        if df.empty or "is_stunted" not in df.columns: continue
        keep = [c for c in KEEP if c in df.columns]
        frames.append(df[keep])
    if not frames: return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    return out[out["age_years"].between(0, 5, inclusive="left")] if "age_years" in out.columns else out


# ── Lazy-loaded module state ────────────────────────────────────────────────────
# NOTE: unlike the original standalone dashboard.py, these are NOT populated at
# import time. Call ensure_loaded() before reading any of these from a callback.

NUTR = CGM = REF = FOLLOWUP = CGM_FU = DIST = MET = STATE = PARTNER = pd.DataFrame()
ALL_DFS: dict = {}
STUNT = pd.DataFrame()

_load_lock = threading.Lock()
_loaded = False


def _reload_all_data():
    """Re-read every DocType from DB into module globals.

    Called on first access (ensure_loaded) and again by the background sync
    thread after ingest completes so the next render picks up fresh data.
    """
    global NUTR, CGM, REF, FOLLOWUP, CGM_FU, DIST, MET, STATE, PARTNER, ALL_DFS, STUNT
    print("\n[ebuzima] Loading data …")
    NUTR     = _load_nutr()
    CGM      = _load_cgm()
    REF      = _load_ref()
    FOLLOWUP = _load_generic("nutrition_followup")
    CGM_FU   = _load_generic("child_growth_monitoring_followup")
    DIST     = _load_generic("nutrition_distribution")
    MET      = _load_generic("nutrition_met_criteria")
    STATE    = _load_generic("nutrition_state")
    PARTNER  = _load_generic("additional_partner_children_detail")
    ALL_DFS  = {
        "nutrition":    NUTR,
        "cgm":          CGM,
        "referrals":    REF,
        "followup":     FOLLOWUP,
        "cgm_followup": CGM_FU,
        "distribution": DIST,
        "met_criteria": MET,
        "state":        STATE,
        "partner":      PARTNER,
    }
    STUNT = _build_stunt()
    total = sum(len(v) for v in ALL_DFS.values())
    print(f"  [ebuzima] Loaded: {total:,} rows across {len(ALL_DFS)} tables  "
          f"· stunting (u5): {len(STUNT):,}")


def ensure_loaded() -> None:
    """Load all DocTypes on first call. Cheap no-op on every call after that."""
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:   # re-check inside the lock (another thread may have won the race)
            return
        _reload_all_data()
        _loaded = True


def dataset_options() -> list:
    """Dropdown options for the 'Dataset' selector — only datasets with data."""
    ensure_loaded()
    opts_all = [
        {"label": "Nutrition (main)",               "value": "nutrition"},
        {"label": "Child Growth Monitoring",        "value": "cgm"},
        {"label": "Nutrition Referral",             "value": "referrals"},
        {"label": "Nutrition Followup",             "value": "followup"},
        {"label": "CGM Followup",                   "value": "cgm_followup"},
        {"label": "Nutrition Distribution",         "value": "distribution"},
        {"label": "Nutrition Met Criteria",         "value": "met_criteria"},
        {"label": "Nutrition State",                "value": "state"},
        {"label": "Additional Partner Children",    "value": "partner"},
    ]
    return [o for o in opts_all if not ALL_DFS.get(o["value"], pd.DataFrame()).empty]


# ══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def opts(s: pd.Series):
    """Dropdown options from a Series — only values with data, sorted."""
    vals = sorted(v for v in s.dropna().unique() if str(v).strip())
    return [{"label": v, "value": v} for v in vals]


def completeness(df: pd.DataFrame) -> pd.Series:
    skip = {"_user_tags","_comments","_assign","_liked_by"}
    cols = [c for c in df.columns if c not in skip]
    return (df[cols].notna() & (df[cols].astype(str) != "")).mean().sort_values() * 100


def dq_score(df: pd.DataFrame) -> float:
    return float(completeness(df).mean()) if not df.empty else 0.0


def _apply(df: pd.DataFrame, provinces=None, districts=None, sectors=None,
           hcs=None, start=None, end=None, under5=False) -> pd.DataFrame:
    if df.empty: return df
    if provinces and "province"  in df.columns: df = df[df["province"].isin(provinces)]
    if districts and "district"  in df.columns: df = df[df["district"].isin(districts)]
    if sectors   and "sector"    in df.columns: df = df[df["sector"].isin(sectors)]
    if hcs       and "company"   in df.columns: df = df[df["company"].isin(hcs)]
    if start     and "creation"  in df.columns: df = df[df["creation"] >= pd.Timestamp(start)]
    if end       and "creation"  in df.columns: df = df[df["creation"] <= pd.Timestamp(end)]
    if under5    and "age_years" in df.columns: df = df[df["age_years"] < 5]
    return df.copy()


def active_df(dataset: str) -> pd.DataFrame:
    ensure_loaded()
    return ALL_DFS.get(dataset, NUTR)


# ══════════════════════════════════════════════════════════════════════════════
# CHART BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def _empty(msg="No data for the selected filters"):
    fig = go.Figure()
    fig.add_annotation(text=msg, xref="paper", yref="paper", x=0.5, y=0.5,
                       showarrow=False, font=dict(size=13, color=C["muted"]))
    fig.update_layout(template=T, paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", height=300)
    return fig


# ── Stunting ──────────────────────────────────────────────────────────────────

def fig_stunting_trend(df: pd.DataFrame):
    if df.empty or "is_stunted" not in df.columns or "month" not in df.columns:
        return _empty("No stunting data available.\nRun ingest.py to populate Nutrition Referral / Followup tables.")
    g = df.groupby("month").agg(
        total=("is_stunted","count"),
        stunted=("is_stunted","sum"),
        sev=("is_sev_stunted","sum") if "is_sev_stunted" in df.columns else ("is_stunted", lambda x: 0),
    ).reset_index().sort_values("month")
    g["pct_stunted"] = g["stunted"] / g["total"] * 100
    g["pct_sev"]     = g["sev"]     / g["total"] * 100
    g["label"]       = g["month"].str[-5:]   # e.g. "01-24"

    fig = go.Figure()
    # Shaded WHO concern zone (>20%)
    fig.add_hrect(y0=20, y1=100, fillcolor=C["warning"], opacity=0.06, line_width=0)
    fig.add_hrect(y0=40, y1=100, fillcolor=C["danger"],  opacity=0.06, line_width=0)
    fig.add_trace(go.Scatter(x=g["month"], y=g["pct_stunted"],
                              name="Stunted (HAZ ≤ −2)",
                              mode="lines+markers",
                              line=dict(color=C["warning"], width=3),
                              marker=dict(size=7),
                              hovertemplate="%{x}<br><b>%{y:.1f}% stunted</b><extra></extra>"))
    fig.add_trace(go.Scatter(x=g["month"], y=g["pct_sev"],
                              name="Severely Stunted (HAZ ≤ −3)",
                              mode="lines+markers",
                              line=dict(color=C["danger"], width=3, dash="dash"),
                              marker=dict(size=7),
                              hovertemplate="%{x}<br><b>%{y:.1f}% severely stunted</b><extra></extra>"))
    fig.add_hline(y=20, line_dash="dot", line_color=C["muted"],
                  annotation_text="WHO concern threshold (20%)",
                  annotation_position="bottom right")
    fig.update_layout(
        title="Stunting Prevalence Trend — Under 5",
        xaxis_title="Month", yaxis_title="% of children assessed",
        template=T, xaxis_tickangle=-45,
        yaxis=dict(range=[0, max(g["pct_stunted"].max() * 1.15, 25)]),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        margin=dict(t=70),
    )
    return fig


def fig_haz_dist(df: pd.DataFrame):
    if df.empty or "haz" not in df.columns: return _empty("No HAZ z-scores available")
    vals = df["haz"].dropna()
    if vals.empty: return _empty()
    fig = px.histogram(vals, nbins=50,
                       title="<b>HAZ (Height-for-Age Z-score) Distribution</b>",
                       template=T, color_discrete_sequence=[C["teal"]])
    fig.add_vrect(x0=-10, x1=-3, fillcolor=C["danger"],  opacity=0.08, line_width=0,
                  annotation_text="Severe", annotation_position="top left")
    fig.add_vrect(x0=-3,  x1=-2, fillcolor=C["warning"], opacity=0.08, line_width=0,
                  annotation_text="Moderate", annotation_position="top left")
    fig.add_vline(x=-3, line_dash="dash", line_color=C["danger"])
    fig.add_vline(x=-2, line_dash="dash", line_color=C["warning"])
    fig.add_vline(x=0,  line_dash="dot",  line_color=C["muted"],
                  annotation_text="WHO median")
    fig.update_layout(xaxis_title="HAZ", yaxis_title="Count", bargap=0.03)
    return fig


def fig_malnutrition(df: pd.DataFrame):
    if df.empty or "mal_cat" not in df.columns: return _empty()
    counts = df["mal_cat"].value_counts()
    fig = px.pie(values=counts.values, names=counts.index,
                 title="<b>Malnutrition Classification</b><br><sup>SAM / MAM / Normal (MUAC + WHZ)</sup>",
                 color=counts.index, color_discrete_map=MAL_COLORS, template=T)
    fig.update_traces(textinfo="percent+label+value",
                      pull=[0.07 if n=="SAM" else 0 for n in counts.index])
    return fig


def fig_muac(df: pd.DataFrame):
    if df.empty or "muac" not in df.columns: return _empty("No MUAC data")
    vals = pd.to_numeric(df["muac"], errors="coerce").dropna()
    if vals.empty: return _empty()
    fig = px.histogram(vals, nbins=30, title="<b>MUAC Distribution</b>",
                       template=T, color_discrete_sequence=[C["warning"]])
    fig.add_vrect(x0=0, x1=11.5, fillcolor=C["danger"], opacity=0.08, line_width=0,
                  annotation_text="SAM")
    fig.add_vrect(x0=11.5, x1=12.5, fillcolor=C["warning"], opacity=0.08, line_width=0,
                  annotation_text="MAM")
    fig.add_vline(x=11.5, line_dash="dash", line_color=C["danger"])
    fig.add_vline(x=12.5, line_dash="dot",  line_color=C["warning"])
    fig.update_layout(xaxis_title="MUAC (cm)", yaxis_title="Count")
    return fig


def fig_whz(df: pd.DataFrame):
    return fig_zscore(df, "w_h_zscore", "Weight-for-Height Z-score (WHZ)")


def fig_zscore(df: pd.DataFrame, col: str, label: str):
    if df.empty or col not in df.columns: return _empty(f"No {col}")
    vals = pd.to_numeric(df[col], errors="coerce").dropna()
    if vals.empty: return _empty()
    fig = px.histogram(vals, nbins=40, title=label,
                       template=T, color_discrete_sequence=[C["teal"]])
    fig.add_vline(x=-2, line_dash="dash", line_color=C["warning"], annotation_text="-2")
    fig.add_vline(x=-3, line_dash="dash", line_color=C["danger"],  annotation_text="-3")
    fig.update_layout(xaxis_title=label, yaxis_title="Count", bargap=0.03)
    return fig


# ── Program / enrollment ──────────────────────────────────────────────────────

def fig_trend(df: pd.DataFrame, title="Monthly Records"):
    if df.empty or "month" not in df.columns: return _empty()
    g = df.groupby("month").size().reset_index(name="n").sort_values("month")
    fig = px.line(g, x="month", y="n", markers=True, title=title,
                  template=T, color_discrete_sequence=[C["primary"]])
    fig.update_layout(xaxis_title="Month", yaxis_title="Records", xaxis_tickangle=-45)
    return fig


def fig_age_hist(df: pd.DataFrame, title="Age Distribution"):
    if df.empty or "age_years" not in df.columns: return _empty()
    vals = df["age_years"].dropna()
    vals = vals[(vals >= 0) & (vals <= 18)]
    if vals.empty: return _empty()
    fig = px.histogram(vals, nbins=72, title=title,
                       template=T, color_discrete_sequence=[C["primary"]])
    fig.add_vline(x=5, line_dash="dash", line_color=C["danger"],
                  annotation_text="Under 5")
    fig.update_layout(xaxis_title="Age (years)", yaxis_title="Count", bargap=0.03)
    return fig


def fig_age_buckets(df: pd.DataFrame, title="Age Groups"):
    if df.empty or "age_bucket" not in df.columns: return _empty()
    order = ["0–1","1–2","2–3","3–4","4–5","5–10","10–18","18+"]
    counts = df["age_bucket"].value_counts().reindex(order, fill_value=0)
    colors = [C["danger"] if b in ("0–1","1–2","2–3","3–4","4–5") else C["primary"]
              for b in order]
    fig = go.Figure(go.Bar(x=counts.index, y=counts.values, marker_color=colors,
                            text=counts.values, textposition="outside"))
    fig.update_layout(title=f"{title}  (red = under 5)",
                      xaxis_title="Age group", yaxis_title="Count", template=T)
    return fig


def fig_gender(df: pd.DataFrame, title="Gender Distribution"):
    if df.empty or "gender" not in df.columns: return _empty()
    counts = df["gender"].value_counts()
    if counts.empty: return _empty()
    fig = px.pie(values=counts.values, names=counts.index, title=title,
                 template=T, color_discrete_sequence=[C["primary"],C["warning"],C["success"]])
    fig.update_traces(textinfo="percent+label")
    return fig


def fig_geo(df: pd.DataFrame, col="district", title=None, top_n=20, color=None):
    if df.empty or col not in df.columns: return _empty()
    counts = df[col].value_counts().head(top_n).sort_values()
    if counts.empty: return _empty()
    fig = px.bar(x=counts.values, y=counts.index, orientation="h",
                 title=title or f"Records by {col.capitalize()}",
                 template=T, color_discrete_sequence=[color or C["primary"]],
                 text_auto=True)
    fig.update_layout(xaxis_title="Records", yaxis_title="")
    return fig


def fig_health_centers(df: pd.DataFrame, top_n=20):
    return fig_geo(df, "company", f"Top {top_n} Health Centers", top_n, C["success"])


def fig_diagnosis(df: pd.DataFrame):
    if df.empty or "diagnosis" not in df.columns: return _empty()
    counts = df["diagnosis"].value_counts().head(15)
    if counts.empty: return _empty()
    fig = px.bar(x=counts.values, y=counts.index, orientation="h",
                 title="Diagnosis Breakdown", template=T,
                 color_discrete_sequence=[C["warning"]], text_auto=True)
    fig.update_layout(xaxis_title="Count", yaxis_title="")
    return fig


# ── Discharge ─────────────────────────────────────────────────────────────────

def fig_active_vs_discharged(df: pd.DataFrame):
    if df.empty or "discharged" not in df.columns: return _empty("No discharge field")
    n_d = int(df["discharged"].sum())
    n_a = len(df) - n_d
    fig = px.pie(values=[n_d, n_a], names=["Discharged","Still Active"],
                 title="<b>Active vs Discharged</b>",
                 hole=0.45, template=T,
                 color_discrete_sequence=[C["success"], C["primary"]])
    fig.update_traces(textinfo="percent+label+value")
    return fig


def fig_discharge_reasons(df: pd.DataFrame):
    if df.empty or "discharging_status" not in df.columns: return _empty()
    counts = df["discharging_status"].dropna().value_counts()
    if counts.empty: return _empty("No discharge status values")
    fig = px.bar(x=counts.values, y=counts.index, orientation="h",
                 title="<b>Discharge Reasons</b>", template=T,
                 color_discrete_sequence=[C["success"]], text_auto=True)
    fig.update_layout(xaxis_title="Count", yaxis_title="")
    return fig


def fig_discharge_trend(df: pd.DataFrame):
    if df.empty or "month" not in df.columns: return _empty()
    enrolled  = df.groupby("month").size().reset_index(name="Enrolled")
    discharged = (df[df["discharged"]].groupby("month").size().reset_index(name="Discharged")
                  if "discharged" in df.columns else pd.DataFrame(columns=["month","Discharged"]))
    g = enrolled.merge(discharged, on="month", how="left").fillna(0).sort_values("month")
    fig = go.Figure()
    fig.add_trace(go.Bar(x=g["month"], y=g["Enrolled"],  name="Enrolled",  marker_color=C["primary"]))
    fig.add_trace(go.Bar(x=g["month"], y=g["Discharged"],name="Discharged",marker_color=C["success"]))
    fig.update_layout(title="<b>Monthly Enrolment vs Discharge</b>",
                      barmode="group", xaxis_title="Month", yaxis_title="Count",
                      template=T, xaxis_tickangle=-45,
                      legend=dict(orientation="h", y=1.1))
    return fig


def fig_time_in_program(df: pd.DataFrame):
    if df.empty or "days_in_program" not in df.columns: return _empty("No time-in-program data")
    discharged = df[df["discharged"]] if "discharged" in df.columns else df
    vals = discharged["days_in_program"].dropna()
    if vals.empty: return _empty()
    med = vals.median()
    fig = px.histogram(vals, nbins=40,
                       title=f"<b>Days in Program (Discharged)</b><br><sup>Median: {med:.0f} days</sup>",
                       template=T, color_discrete_sequence=[C["purple"]])
    fig.add_vline(x=med, line_dash="dash", line_color=C["danger"])
    fig.update_layout(xaxis_title="Days", yaxis_title="Count", bargap=0.04)
    return fig


def fig_discharge_by_district(df: pd.DataFrame):
    if df.empty or "discharged" not in df.columns or "district" not in df.columns:
        return _empty()
    g = (df.groupby("district")
         .agg(total=("discharged","count"), discharged=("discharged","sum"))
         .reset_index())
    g["pct"] = g["discharged"] / g["total"] * 100
    g = g[g["total"] >= 5].sort_values("pct").tail(20)
    fig = px.bar(x=g["pct"], y=g["district"], orientation="h",
                 title="<b>Discharge Rate by District</b>",
                 template=T, color_discrete_sequence=[C["success"]],
                 text=[f"{p:.1f}%" for p in g["pct"]])
    fig.update_layout(xaxis=dict(title="% Discharged", range=[0,115]),
                      yaxis_title="")
    return fig


# ── Birth weight ──────────────────────────────────────────────────────────────

def fig_birth_weight(df: pd.DataFrame):
    if df.empty or "bw_clean" not in df.columns: return _empty()
    bw = df["bw_clean"].dropna()
    if bw.empty: return _empty()
    fig = px.histogram(bw, nbins=50, title="<b>Birth Weight Distribution</b>",
                       template=T, color_discrete_sequence=[C["primary"]])
    fig.add_vline(x=2500, line_dash="dash", line_color=C["danger"],
                  annotation_text="LBW threshold")
    fig.update_layout(xaxis_title="Birth weight (grams)", yaxis_title="Count", bargap=0.03)
    return fig


def fig_lbw_pie(df: pd.DataFrame):
    if df.empty or "lbw" not in df.columns: return _empty()
    clean = df.dropna(subset=["bw_clean"])
    if clean.empty: return _empty()
    lbw_n = int(clean["lbw"].sum())
    fig = px.pie(values=[lbw_n, len(clean)-lbw_n],
                 names=["LBW (<2500g)","Normal (≥2500g)"],
                 title="<b>Low Birth Weight Prevalence</b>", template=T,
                 color_discrete_sequence=[C["danger"], C["success"]])
    fig.update_traces(textinfo="percent+label+value")
    return fig


# ── Data quality ──────────────────────────────────────────────────────────────

def fig_completeness(df: pd.DataFrame, title="Field Completeness (%)"):
    if df.empty: return _empty()
    comp = completeness(df).tail(30)
    colors = [C["success"] if v>=90 else C["warning"] if v>=60 else C["danger"]
              for v in comp.values]
    fig = go.Figure(go.Bar(x=comp.values, y=comp.index, orientation="h",
                            marker_color=colors,
                            text=[f"{v:.1f}%" for v in comp.values],
                            textposition="outside"))
    fig.update_layout(title=title, template=T,
                      xaxis=dict(title="Completeness (%)", range=[0,115]),
                      yaxis_title="")
    return fig


def fig_missing_critical(df: pd.DataFrame, cols: list):
    present = [c for c in cols if c in df.columns]
    if not present or df.empty: return _empty()
    miss = (df[present].isna().mean() * 100).sort_values(ascending=False)
    colors = [C["danger"] if v>20 else C["warning"] if v>5 else C["success"]
              for v in miss.values]
    fig = go.Figure(go.Bar(x=miss.index, y=miss.values, marker_color=colors,
                            text=[f"{v:.1f}%" for v in miss.values],
                            textposition="outside"))
    fig.update_layout(title="Missing Values — Critical Fields", template=T,
                      yaxis=dict(title="Missing (%)", range=[0,115]))
    return fig


# ── Stunting (district/age/gender) — kept identical to the tab-builder variants
# used in the original dashboard.py, which is what actually renders on the page ──

def fig_stunting_district(df: pd.DataFrame):
    if df.empty or "is_stunted" not in df.columns or "district" not in df.columns:
        return _empty()
    g = (df.groupby("district")
         .agg(total=("is_stunted","count"), stunted=("is_stunted","sum"))
         .reset_index())
    g["pct"] = g["stunted"] / g["total"] * 100
    g = g[g["total"] >= 5].sort_values("pct").tail(20)
    if g.empty: return _empty("No districts with ≥5 records")
    colors = [C["danger"] if p>=40 else C["warning"] if p>=20 else C["success"]
              for p in g["pct"]]
    fig = go.Figure(go.Bar(
        x=g["pct"], y=g["district"], orientation="h",
        marker_color=colors,
        text=[f"{p:.1f}%  ({int(s)}/{int(t)})" for p,s,t in zip(g["pct"],g["stunted"],g["total"])],
        textposition="outside",
    ))
    fig.update_layout(
        title="<b>Stunting Hotspots by District</b><br><sup>Red ≥40%  Orange ≥20%  Green <20%</sup>",
        xaxis=dict(title="% Stunted (Under 5)", range=[0,115]),
        yaxis_title="", template=T,
    )
    return fig


def fig_stunting_age(df: pd.DataFrame):
    if df.empty or "stunting_cat" not in df.columns or "age_bucket" not in df.columns:
        return _empty()
    order  = ["0–1","1–2","2–3","3–4","4–5"]
    cat_or = ["Normal","Moderate Stunting","Severe Stunting"]
    g = (df[df["stunting_cat"].isin(cat_or)]
         .groupby(["age_bucket","stunting_cat"]).size().reset_index(name="n"))
    if g.empty: return _empty()
    fig = px.bar(g[g["age_bucket"].isin(order)],
                 x="age_bucket", y="n", color="stunting_cat",
                 color_discrete_map=STUNT_COLORS,
                 category_orders={"age_bucket":order,"stunting_cat":cat_or},
                 barmode="stack", title="<b>Stunting by Age Group (Under 5)</b>",
                 template=T, text_auto=True)
    fig.update_layout(xaxis_title="Age group (years)", yaxis_title="Children",
                      legend_title="", legend=dict(orientation="h", y=1.1))
    return fig


def fig_stunting_gender(df: pd.DataFrame):
    if df.empty or "is_stunted" not in df.columns or "gender" not in df.columns:
        return _empty()
    g = (df.groupby("gender")
         .agg(total=("is_stunted","count"), stunted=("is_stunted","sum")).reset_index())
    g["pct"] = g["stunted"] / g["total"] * 100
    fig = px.bar(g, x="gender", y="pct", text=[f"{p:.1f}%" for p in g["pct"]],
                 color="gender", template=T,
                 color_discrete_sequence=[C["primary"],C["warning"]],
                 title="<b>Stunting Prevalence by Gender</b>")
    fig.update_layout(yaxis=dict(title="% Stunted",range=[0,105]),
                      xaxis_title="", showlegend=False)
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# KPI CARD
# ══════════════════════════════════════════════════════════════════════════════

def card(title, value, sub="", color=None):
    from dash import html
    color = color or C["primary"]
    return html.Div([
        html.P(title, style={"margin":"0 0 3px","color":C["muted"],"fontSize":"11px",
                              "fontWeight":"700","textTransform":"uppercase",
                              "letterSpacing":"0.5px"}),
        html.H3(str(value), style={"margin":"0","color":color,
                                    "fontSize":"24px","fontWeight":"700"}),
        html.P(sub, style={"margin":"3px 0 0","color":C["muted"],"fontSize":"11px"}),
    ], style={"background":C["white"],"border":f"1px solid {C['border']}",
              "borderTop":f"4px solid {color}","borderRadius":"7px",
              "padding":"12px 16px","flex":"1","minWidth":"130px",
              "boxShadow":"0 1px 4px rgba(0,0,0,.06)"})


# ══════════════════════════════════════════════════════════════════════════════
# TAB BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def _row(*figs, h="360px"):
    from dash import html, dcc
    return html.Div(
        [dcc.Graph(figure=f, style={"height":h,"flex":"1","minWidth":"290px"})
         for f in figs],
        style={"display":"flex","gap":"12px","flexWrap":"wrap","marginBottom":"12px"}
    )


def _sec(title, *content):
    from dash import html
    return html.Div([
        html.H5(title, style={"color":C["primary"],"margin":"0 0 8px","fontSize":"13px",
                               "fontWeight":"700","borderBottom":f"2px solid {C['border']}",
                               "paddingBottom":"5px"}),
        *content,
    ], style={"marginBottom":"18px"})


def tab_stunting(stunt, nutr):
    from dash import html
    return html.Div([
        _sec("Stunting Trend Over Time",
             _row(fig_stunting_trend(stunt), h="380px")),
        _sec("Classification & Distribution",
             _row(fig_stunting_age(stunt), fig_haz_dist(stunt))),
        _sec("Malnutrition Status (MUAC + WHZ)",
             _row(fig_malnutrition(stunt), fig_muac(stunt), fig_whz(stunt))),
        _sec("Geographic Hotspots & Gender",
             _row(fig_stunting_district(stunt), fig_stunting_gender(stunt))),
    ])


def tab_discharge(nutr):
    from dash import html
    if nutr.empty: return html.P("No Nutrition data.", style={"color":C["muted"]})
    return html.Div([
        _sec("Program Status",
             _row(fig_active_vs_discharged(nutr), fig_discharge_reasons(nutr))),
        _sec("Trends & Time in Program",
             _row(fig_discharge_trend(nutr), fig_time_in_program(nutr))),
        _sec("Geographic Discharge Rates",
             _row(fig_discharge_by_district(nutr),
                  fig_geo(nutr[nutr["discharged"]] if "discharged" in nutr.columns else nutr,
                          "sector","Discharged by Sector", color=C["success"]))),
    ])


def tab_cgm(cgm):
    from dash import html
    if cgm.empty: return html.P("No CGM data.", style={"color":C["muted"]})
    return html.Div([
        _sec("Birth Metrics",
             _row(fig_birth_weight(cgm), fig_lbw_pie(cgm))),
        _sec("Demographics",
             _row(fig_age_buckets(cgm,"CGM Age Groups"), fig_gender(cgm,"CGM Gender"))),
        _sec("Registration Trends",
             _row(fig_trend(cgm,"Monthly CGM Registrations"),
                  fig_geo(cgm,"district","CGM by District"))),
    ])


def tab_overview(nutr, filt):
    from dash import html
    fu   = _apply(FOLLOWUP, **filt)
    dist = _apply(DIST, **filt)
    met  = _apply(MET,  **filt)
    return html.Div([
        _sec("Nutrition Enrolment",
             _row(fig_trend(nutr,"Monthly Enrolment"), h="340px")),
        _sec("Program Profile",
             _row(fig_diagnosis(nutr), fig_age_buckets(nutr))),
        _sec("Followup & Distribution",
             _row(fig_trend(fu,"Nutrition Followup Visits") if not fu.empty
                  else _empty("No followup data — run ingest"),
                  fig_trend(dist,"Nutrition Distribution") if not dist.empty
                  else _empty("No distribution data"))),
        _sec("Met Criteria & Other",
             _row(fig_trend(met,"Met Criteria Records") if not met.empty
                  else _empty("No met-criteria data"),
                  fig_geo(nutr,"company","Top Health Centers by Enrolment"))),
    ])


def tab_geo(df, stunt):
    from dash import html
    return html.Div([
        _sec("Stunting Hotspots (Under 5)",
             _row(fig_stunting_district(stunt), h="420px")),
        _sec("Enrolment by Location",
             _row(fig_geo(df,"province","Records by Province") if "province" in df.columns
                  else _empty("No province data"),
                  fig_geo(df,"district","Records by District"))),
        _sec("Sector & Health Center",
             _row(fig_geo(df,"sector","Records by Sector", color=C["teal"]),
                  fig_health_centers(df))),
    ])


def tab_demo(df):
    from dash import html
    return html.Div([
        _sec("Age Distribution",
             _row(fig_age_hist(df), fig_age_buckets(df))),
        _sec("Gender",
             _row(fig_gender(df),
                  fig_active_vs_discharged(df) if "discharged" in df.columns
                  else fig_diagnosis(df))),
    ])


def tab_dq(df):
    from dash import html
    if df.empty: return html.P("No data.", style={"color":C["muted"]})
    dq  = dq_score(df)
    col = C["success"] if dq>=80 else C["warning"] if dq>=60 else C["danger"]
    critical = ["date_of_birth","gender","district","sector","company",
                "creation","diagnosis","h_a_zscore","birth_weight","discharge_date"]
    return html.Div([
        html.Div([
            html.H4("Overall Data Quality Score",style={"margin":"0 0 5px"}),
            html.H2(f"{dq:.1f}%",style={"color":col,"margin":"0 0 3px",
                                          "fontSize":"40px","fontWeight":"700"}),
            html.P("Mean completeness across all non-system fields",
                   style={"color":C["muted"],"margin":0,"fontSize":"12px"}),
        ], style={"background":C["white"],"border":f"1px solid {C['border']}",
                   "borderLeft":f"6px solid {col}","borderRadius":"7px",
                   "padding":"16px 20px","marginBottom":"12px"}),
        *([] if "bw_suspect" not in df.columns else [
            html.Div([
                html.B("⚠ Suspect birth weights: ", style={"color":C["danger"]}),
                html.Span(f"{int(df['bw_suspect'].sum()):,} records ({df['bw_suspect'].mean()*100:.1f}%) "
                          "have birth_weight between 6–100 (ambiguous unit — not clearly grams or kg)"),
            ], style={"background":"#fff3f3","border":f"1px solid {C['danger']}",
                       "borderRadius":"6px","padding":"10px 14px","marginBottom":"12px"})
        ]),
        _row(fig_missing_critical(df, critical), fig_completeness(df), h="420px"),
    ])


# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND INGEST
# ══════════════════════════════════════════════════════════════════════════════

_ingest_lock  = threading.Lock()
_ingest_state: dict = {
    "running":  False,
    "started":  None,
    "finished": None,
    "results":  None,
}


def run_ingest_bg():
    """Incremental ingest for all DocTypes — runs in a background thread."""
    from .ingest import TARGET_DOCTYPES, ingest_doctype
    results = []
    for dt in TARGET_DOCTYPES:
        try:
            r = ingest_doctype(dt, incremental=True)
            results.append(r)
        except Exception as e:
            results.append({"doctype": dt, "fetched": 0, "inserted": 0,
                            "updated": 0, "stored": 0, "error": str(e)})
    # Refresh in-memory DataFrames from DB so the next render shows updated data
    try:
        _reload_all_data()
    except Exception as e:
        print(f"  [ebuzima] [warn] data reload after sync failed: {e}")
    with _ingest_lock:
        _ingest_state.update({
            "running":  False,
            "finished": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
            "results":  results,
        })
