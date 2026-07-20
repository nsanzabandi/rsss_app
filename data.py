"""
data.py — Thread-safe data cache + filter helpers for the RSSS dashboard.

Data source priority:
  1. Local PostgreSQL (immunization_db — immunization_vaccination table)
  2. CSV fallback (data/combined_df.csv) if DB is unreachable

The cache holds 1 hour TTL so the server doesn't query Postgres on every callback.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

# ── Constants ──────────────────────────────────────────────────────────────────

TEI        = "tracked_entity_instance"
_CSV_PATH  = Path(__file__).parent / "data" / "combined_df.csv"
_CACHE_TTL = 3600   # 1 hour

_cache: dict = {}
_lock  = threading.Lock()


# ── SQL query (local immunization_db) ─────────────────────────────────────────

_SQL = """
SELECT
    entity_id                AS tracked_entity_instance,
    child_id,
    first_name,
    family_name,
    child_name,
    date_of_birth,
    gender,
    residence_province       AS province,
    COALESCE(h_district, residence_district, district_source) AS district,
    h_district_hospital      AS district_hospital,
    health_facility,
    h_sector                 AS sector,
    last_immunization_date   AS immunization_date,
    age_visit_months         AS age_in_months,
    weight_visit_kg          AS weight_at_visit_kg,
    height_visit_cm          AS height_at_visit_cm,
    muac_cm,
    stunting_status,
    wasting_status,
    weight_status,
    immunization_schedule,
    next_visit_date,
    bcg, opv, hep_b_birth, dpt_hepb_hib,
    pneumococcal, rotavirus, measles_rubella, hpv,
    mother_names, mother_phone,
    father_names, father_phone,
    district_source,
    last_updated_on
FROM immunization_vaccination
WHERE stunting_status IS NOT NULL
  AND LOWER(TRIM(stunting_status)) LIKE '%stunt%'
  AND LOWER(TRIM(stunting_status)) NOT LIKE 'not %'
  AND LOWER(TRIM(stunting_status)) NOT LIKE 'no %'
"""


# ── Loaders ────────────────────────────────────────────────────────────────────

def _load_from_db() -> pd.DataFrame | None:
    """Query local immunization_db. Returns None on failure."""
    try:
        from config.db_local import get_local_conn
        import psycopg2
        conn = get_local_conn()
        print("[data] Loading stunted records from local PostgreSQL…")
        cur = conn.cursor()
        cur.execute(_SQL)
        cols = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        if not rows:
            # Diagnose WHY: show what stunting_status actually contains so the
            # filter can be matched to the real values instead of guessing.
            try:
                cur.execute("""
                    SELECT stunting_status, COUNT(*) AS n
                    FROM immunization_vaccination
                    GROUP BY stunting_status ORDER BY n DESC LIMIT 12
                """)
                vals = cur.fetchall()
                print("[data] Local DB returned 0 stunted rows. "
                      "Actual stunting_status values:")
                for v, n in vals:
                    print(f"        {v!r}: {n:,}")
            except Exception:
                pass
            cur.close(); conn.close()
            print("[data] Falling back to CSV.")
            return None
        cur.close()
        conn.close()
        df = pd.DataFrame(rows, columns=cols)
        df = _normalise(df)
        print(f"[data] {len(df):,} stunted records loaded from PostgreSQL.")
        return df
    except Exception as exc:
        print(f"[data] PostgreSQL unavailable ({exc}) — will try CSV fallback.")
        return None


def _load_from_csv() -> pd.DataFrame | None:
    """Read combined_df.csv (legacy fallback)."""
    if not _CSV_PATH.exists():
        print(f"[data] CSV not found: {_CSV_PATH}")
        return None
    print(f"[data] Loading {_CSV_PATH} …")
    df = pd.read_csv(_CSV_PATH, low_memory=False)
    df = _normalise(df)
    # Filter to stunted only (contains "stunt", excluding normal / not-stunted)
    if "stunting_status" in df.columns:
        ss = df["stunting_status"].astype(str).str.lower().str.strip()
        df = df[ss.str.contains("stunt", na=False)
                & ~ss.str.startswith("not ")
                & ~ss.str.startswith("no ")].copy()
    elif "height_for_age_zscore" in df.columns:
        df = df[pd.to_numeric(df["height_for_age_zscore"], errors="coerce") <= -2].copy()
    print(f"[data] {len(df):,} stunted records loaded from CSV.")
    return df


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Common column normalisation regardless of source."""
    # Dates
    for col in ["immunization_date", "next_visit_date", "date_of_birth"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    # String columns — strip and replace empty/null strings
    str_cols = [
        "province", "district", "district_hospital", "health_facility",
        "gender", "stunting_status", "wasting_status", "weight_status",
        "immunization_schedule", "sector", "muac_cm",
        "bcg", "opv", "hep_b_birth", "dpt_hepb_hib",
        "pneumococcal", "rotavirus", "measles_rubella", "hpv",
    ]
    for col in str_cols:
        if col in df.columns:
            df[col] = (
                df[col].astype(str)
                .replace(["nan", "NaN", "None", "null", "none", ""], np.nan)
                .str.strip()
            )

    # Canonical gender — merge 'M'/'Male' and 'F'/'Female' so they aren't split.
    if "gender" in df.columns:
        g = df["gender"].astype(str).str.strip().str.lower()
        df["gender"] = np.where(g.str.startswith("m"), "Male",
                        np.where(g.str.startswith("f"), "Female", None))

    # Numeric columns
    for col in ["age_in_months", "weight_at_visit_kg", "height_at_visit_cm",
                "height_for_age_zscore"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # ── Measurement plausibility cleaning (WHO child growth standards) ────────
    # Values outside physiological range for children 0–72 months are set to NaN.
    # Ranges are conservative; extreme outliers are almost always data-entry errors.
    if "weight_at_visit_kg" in df.columns:
        w = df["weight_at_visit_kg"]
        df.loc[(w < 1.5) | (w > 35), "weight_at_visit_kg"] = np.nan

    if "height_at_visit_cm" in df.columns:
        h = df["height_at_visit_cm"]
        df.loc[(h < 40) | (h > 130), "height_at_visit_cm"] = np.nan

    if "age_in_months" in df.columns:
        a = df["age_in_months"]
        df.loc[(a < 0) | (a > 72), "age_in_months"] = np.nan

    if "height_for_age_zscore" in df.columns:
        z = df["height_for_age_zscore"]
        # HAZ outside ±6 SD is biologically implausible (WHO flag criterion)
        df.loc[(z < -6) | (z > 6), "height_for_age_zscore"] = np.nan

    # Back-fill province via district → most common province mapping
    if "province" in df.columns and "district" in df.columns:
        prov_map = (
            df[df["province"].notna() & df["district"].notna()]
            .groupby("district")["province"]
            .agg(lambda s: s.mode().iloc[0] if not s.mode().empty else np.nan)
        )
        missing = df["province"].isna() & df["district"].notna()
        df.loc[missing, "province"] = df.loc[missing, "district"].map(prov_map)

    # Fallback: fill health_facility from "Organisation unit name" (CSV only)
    if "Organisation unit name" in df.columns and "health_facility" in df.columns:
        df["health_facility"] = df["health_facility"].fillna(df["Organisation unit name"])

    return df


# ── Public cache API ───────────────────────────────────────────────────────────

def get_df() -> pd.DataFrame | None:
    """Return cached stunted-children DataFrame, reloading if stale."""
    with _lock:
        if not ("df" in _cache and (time.time() - _cache.get("ts", 0)) < _CACHE_TTL):
            df = _load_from_db()
            if df is None:
                df = _load_from_csv()
            _cache["df"]     = df
            _cache["ts"]     = time.time()
            _cache["source"] = "postgresql" if df is not None and len(df) > 0 else "csv"
        return _cache["df"]


def get_data_source() -> str:
    """Return 'postgresql' or 'csv' depending on which source is active."""
    with _lock:
        return _cache.get("source", "unknown")


def invalidate_cache() -> None:
    """Force a full reload of every cached table (call after an ETL sync/month load)."""
    with _lock:
        _cache.clear()
    for lock_name, cache_name in (("_all_lock", "_all_cache"),
                                   ("_meas_lock", "_meas_cache"),
                                   ("_child_lock", "_child_cache")):
        try:
            lock = globals()[lock_name]
            with lock:
                globals()[cache_name].clear()
        except (KeyError, NameError):
            pass
    try:
        _child_state["status"] = "idle"
    except (NameError, TypeError):
        pass


# ── All-children loader (denominators for stunting RATE / prevalence) ──────────
#
# get_df() returns STUNTED children only (the numerator). To compute a real
# stunting rate (stunted unique children ÷ all vaccinated unique children) or a
# prevalence-based geographic hotspot map, we also need the full vaccinated
# population. get_all_df() loads that, restricted to the columns the overview
# analyses need, with its own cache. It is filterable with the same
# filter_by_user / filter_geo / filter_by_date helpers.

# Columns needed for denominator analyses (kept small to limit memory on CSV).
_ALL_COLS = [
    "tracked_entity_instance", "immunization_date",
    "province", "district", "district_hospital", "health_facility", "sector",
    "stunting_status", "severe_stunting", "age_in_months",
]

_SQL_ALL = """
SELECT
    entity_id                AS tracked_entity_instance,
    residence_province       AS province,
    COALESCE(h_district, residence_district, district_source) AS district,
    h_district_hospital      AS district_hospital,
    health_facility,
    h_sector                 AS sector,
    last_immunization_date   AS immunization_date,
    age_visit_months         AS age_in_months,
    stunting_status
FROM immunization_vaccination
WHERE last_immunization_date IS NOT NULL
"""

_all_cache: dict = {}
_all_lock = threading.Lock()


def _derive_severe(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure a normalised 'severe_stunting' Yes/No column exists."""
    if "severe_stunting" in df.columns:
        df["severe_stunting"] = (
            df["severe_stunting"].astype(str).str.strip().str.lower()
            .map({"yes": "Yes", "true": "Yes", "1": "Yes",
                  "severe": "Yes", "severely stunted": "Yes"})
            .fillna("No")
        )
    elif "stunting_status" in df.columns:
        df["severe_stunting"] = (
            df["stunting_status"].astype(str).str.lower()
            .str.contains("sever").map({True: "Yes", False: "No"}).fillna("No")
        )
    return df


def _load_all_from_db() -> pd.DataFrame | None:
    try:
        from config.db_local import get_local_conn
        conn = get_local_conn()
        print("[data] Loading ALL vaccinated children from local PostgreSQL…")
        cur = conn.cursor()
        cur.execute(_SQL_ALL)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        cur.close(); conn.close()
        if not rows:
            print("[data] All-children query returned 0 rows — trying CSV.")
            return None
        df = _derive_severe(_normalise(pd.DataFrame(rows, columns=cols)))
        print(f"[data] {len(df):,} total records (all children) from PostgreSQL.")
        return df
    except Exception as exc:
        print(f"[data] PostgreSQL all-children unavailable ({exc}) — trying CSV.")
        return None


def _load_all_from_csv() -> pd.DataFrame | None:
    if not _CSV_PATH.exists():
        return None
    print(f"[data] Loading ALL children from {_CSV_PATH} (selected columns)…")
    # usecols keeps memory in check on the ~2.4M-row CSV; only load what exists.
    import csv as _csv
    with open(_CSV_PATH, newline="") as f:
        header = next(_csv.reader(f))
    use = [c for c in _ALL_COLS if c in header]
    df = pd.read_csv(_CSV_PATH, usecols=use, low_memory=False)
    df = _derive_severe(_normalise(df))
    print(f"[data] {len(df):,} total records (all children) from CSV.")
    return df


def get_all_df() -> pd.DataFrame | None:
    """Return cached all-vaccinated-children DataFrame (denominator), reloading
    if stale. Falls back to CSV when the local DB is empty/unreachable."""
    with _all_lock:
        if not ("df" in _all_cache and (time.time() - _all_cache.get("ts", 0)) < _CACHE_TTL):
            df = _load_all_from_db()
            if df is None:
                df = _load_all_from_csv()
            _all_cache["df"] = df
            _all_cache["ts"] = time.time()
        return _all_cache["df"]


# ── Raw measurements + computed-vs-column comparison (cached) ──────────────────
# These power the dashboard's "computed stunting vs column" validation panel.
# Heavy, so the RESULT is cached and only built on demand (button click).

_MEAS_SQL = """
SELECT
    entity_id                AS tracked_entity_instance,
    gender,
    date_of_birth,
    last_immunization_date   AS immunization_date,
    next_visit_date,
    age_visit_months         AS age_in_months,
    height_visit_cm          AS height_at_visit_cm,
    weight_visit_kg          AS weight_at_visit_kg,
    stunting_status,
    immunization_schedule,
    bcg, opv, hep_b_birth, dpt_hepb_hib,
    pneumococcal, rotavirus, measles_rubella, hpv,
    COALESCE(h_district, residence_district, district_source) AS district,
    h_district_hospital      AS district_hospital,
    residence_province       AS province
FROM immunization_vaccination
"""

_MEAS_CSV_COLS = [
    "tracked_entity_instance", "gender", "dob", "date_of_birth",
    "immunization_date", "next_visit_date", "age_in_months",
    "height_at_visit_cm", "weight_at_visit_kg", "stunting_status",
    "immunization_schedule",
    "vaccine_6_weeks", "vaccine_10_weeks", "vaccine_14_weeks", "measles_rubella",
    "district", "district_hospital", "province",
]

_meas_cache: dict = {}
_meas_lock = threading.Lock()


def _load_measurements() -> pd.DataFrame | None:
    try:
        from config.db_local import get_local_conn
        conn = get_local_conn()
        cur = conn.cursor()
        cur.execute(_MEAS_SQL)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        cur.close(); conn.close()
        if not rows:
            raise ValueError("0 rows")
        df = pd.DataFrame(rows, columns=cols)
        print(f"[data] {len(df):,} raw measurement rows from PostgreSQL.")
        return df
    except Exception as exc:
        print(f"[data] Measurements from DB unavailable ({exc}) — trying CSV.")
        if not _CSV_PATH.exists():
            return None
        import csv as _csv
        with open(_CSV_PATH, newline="") as f:
            header = next(_csv.reader(f))
        use = [c for c in _MEAS_CSV_COLS if c in header]
        df = pd.read_csv(_CSV_PATH, usecols=use, low_memory=False)
        print(f"[data] {len(df):,} raw measurement rows from CSV.")
        return df


def get_measurements_df() -> pd.DataFrame | None:
    with _meas_lock:
        if not ("df" in _meas_cache and (time.time() - _meas_cache.get("ts", 0)) < _CACHE_TTL):
            _meas_cache["df"] = _load_measurements()
            _meas_cache["ts"] = time.time()
        return _meas_cache["df"]




# ── Computed per-child table + monthly trend (built once, warmed, cached) ──────
# The heavy WHO computation runs ONCE here, producing:
#   • child_df  — one row per child (current status, ever, severe, missed, risk,
#                 geo, latest visit date). Filtering this is instant, so the KPI
#                 cards and Overview charts respond to province/district/date.
#   • monthly_df— month × geo aggregates (children measured + stunted) for the
#                 stunting-rate trend, with future/partial months excluded.

_child_cache: dict = {}
_child_lock = threading.Lock()
_child_state = {"status": "idle"}   # idle | running | done | error


def _build_child_and_monthly(meas: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    from core.stunting_calculator import add_computed_stunting, _resolve

    v = add_computed_stunting(meas)
    rc = _resolve(v)
    tei, date = rc["tei"], rc["date"]
    v["_o"] = pd.to_datetime(v[date], errors="coerce") if date else pd.NaT
    v["_stunted"] = v["haz_calc"] < -2
    v["_severe"]  = v["haz_calc"] < -3

    geo = [c for c in ("province", "district", "district_hospital") if c in v.columns]

    # ── one row per child = latest visit overall (keeps geo even if HAZ null) ──
    latest_all = (v.sort_values("_o").drop_duplicates(subset=[tei], keep="last")).copy()

    # current status from latest VALID (HAZ-computable) visit
    valid = v[v["haz_calc"].notna()]
    latest_valid = (valid.sort_values("_o")
                         .drop_duplicates(subset=[tei], keep="last")
                         .set_index(tei))
    ever = (valid.assign(_s=valid["haz_calc"] < -2).groupby(tei)["_s"].max())

    base = latest_all.set_index(tei)
    child = base[geo].copy()
    child["immunization_date"] = base["_o"]               # latest visit date
    child["age_months_calc"]   = base["age_months_calc"]
    child["is_stunted"]   = latest_valid["haz_calc"] < -2
    child["is_severe"]    = latest_valid["haz_calc"] < -3
    child["ever_stunted"] = ever.reindex(child.index).eq(True)
    child["classified"]   = child["is_stunted"].notna()

    # missed doses (latest visit) + under-risk flags
    child["missed"] = False
    try:
        from core.risk_classifier import flag_missed_vaccinations
        fl = flag_missed_vaccinations(latest_all)
        if "missed_count" in fl.columns:
            child["missed"] = ((fl.set_index(tei)["missed_count"] > 0)
                               .reindex(child.index).eq(True))
    except Exception as exc:
        print(f"[data] missed-doses skipped: {exc}")

    child["under_risk"] = False
    try:
        from core.risk_classifier import classify_at_risk
        at = classify_at_risk(meas)
        rtei = "entity_id" if "entity_id" in at.columns else TEI
        if rtei in at.columns:
            child["under_risk"] = child.index.isin(set(at[rtei].dropna().unique()))
    except Exception as exc:
        print(f"[data] under-risk skipped: {exc}")

    child = child.reset_index().rename(columns={tei: "tracked_entity_instance"})

    # ── monthly × geo aggregates for the trend (computed stunting) ────────────
    today = pd.Timestamp.today().normalize()
    vv = valid[valid["_o"].notna() & (valid["_o"] <= today)].copy()
    vv["month"] = vv["_o"].dt.to_period("M").dt.to_timestamp()
    gcols = ["month"] + geo
    monthly = (vv.groupby(gcols)
                 .agg(measured=(tei, "nunique"),
                      stunted=("_stunted", "sum"),
                      severe=("_severe", "sum"))
                 .reset_index())

    # ── stunting by immunization schedule (EPI visit) × geo ───────────────────
    schedule = pd.DataFrame()
    if "immunization_schedule" in valid.columns:
        sv = valid[valid["immunization_schedule"].notna()].copy()
        scols = ["immunization_schedule"] + geo
        schedule = (sv.groupby(scols)
                      .agg(measured=(tei, "nunique"),
                           stunted=("_stunted", "sum"),
                           severe=("_severe", "sum"))
                      .reset_index())
    return child, monthly, schedule


def get_child_df() -> pd.DataFrame | None:
    with _child_lock:
        if "child" in _child_cache and (time.time() - _child_cache.get("ts", 0)) < _CACHE_TTL:
            return _child_cache["child"]
    return None


def get_monthly_df() -> pd.DataFrame | None:
    with _child_lock:
        if "monthly" in _child_cache and (time.time() - _child_cache.get("ts", 0)) < _CACHE_TTL:
            return _child_cache["monthly"]
    return None


def get_schedule_df() -> pd.DataFrame | None:
    with _child_lock:
        if "schedule" in _child_cache and (time.time() - _child_cache.get("ts", 0)) < _CACHE_TTL:
            return _child_cache["schedule"]
    return None


def child_build_status() -> str:
    with _child_lock:
        return _child_state["status"]


def warm_child_level() -> None:
    """Build the per-child table + monthly trend in the background (idempotent)."""
    with _child_lock:
        if _child_state["status"] == "running":
            return
        if "child" in _child_cache and (time.time() - _child_cache.get("ts", 0)) < _CACHE_TTL:
            return
        _child_state["status"] = "running"

    def _worker():
        try:
            meas = get_measurements_df()
            if meas is None or meas.empty:
                with _child_lock:
                    _child_state["status"] = "error"
                return
            child, monthly, schedule = _build_child_and_monthly(meas)
            with _child_lock:
                _child_cache["child"]    = child
                _child_cache["monthly"]  = monthly
                _child_cache["schedule"] = schedule
                _child_cache["ts"]       = time.time()
                _child_state["status"]   = "done"
            print(f"[data] Child-level table ready ({len(child):,} children).")
        except Exception as exc:
            import traceback; traceback.print_exc()
            with _child_lock:
                _child_state["status"] = "error"
            print(f"[data] Child-level build failed: {exc}")

    threading.Thread(target=_worker, daemon=True).start()


def summarize_children(child: pd.DataFrame) -> dict:
    """Compute the KPI numbers from a (possibly filtered) child-level frame."""
    if child is None or child.empty:
        return dict(total_vaccinated=0, current_total=0, current_severe=0,
                    ever_stunted=0, unclassified=0, stunting_pct=0.0,
                    missed_doses=0, under_risk=0)

    def _sum(colname):
        if colname not in child.columns:
            return 0
        return int(child[colname].eq(True).sum())

    tv = len(child)
    cur = _sum("is_stunted")
    unclassified = int(child["is_stunted"].isna().sum())
    assessed = tv - unclassified          # children with a usable height/DOB
    return dict(
        total_vaccinated=tv,
        assessed=assessed,
        current_total=cur,
        current_severe=_sum("is_severe"),
        ever_stunted=_sum("ever_stunted"),
        unclassified=unclassified,
        # Prevalence is among ASSESSED children — you can't classify a child
        # with no height/DOB, so they don't belong in the denominator.
        stunting_pct=round(cur / max(assessed, 1) * 100, 1),
        missed_doses=_sum("missed"),
        under_risk=_sum("under_risk"),
    )


# ── Role-based filter ──────────────────────────────────────────────────────────

def filter_by_user(df: pd.DataFrame, user: dict) -> pd.DataFrame:
    if df is None or df.empty or not user:
        return df
    role     = user.get("role", "")
    district = user.get("district")
    hospital = user.get("hospital")
    hc       = user.get("health_center")

    if role == "ministry":
        return df
    if role == "district" and district and "district" in df.columns:
        return df[df["district"] == district].copy()
    if role == "hospital" and hospital and "district_hospital" in df.columns:
        return df[df["district_hospital"] == hospital].copy()
    if role == "health_center" and hc and "health_facility" in df.columns:
        return df[df["health_facility"] == hc].copy()
    return df


# ── Date filter ────────────────────────────────────────────────────────────────

def filter_by_date(df: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    if df is None or df.empty or "immunization_date" not in df.columns:
        return df
    if not start and not end:
        return df
    mask_no_date = df["immunization_date"].isna()
    dates = df["immunization_date"]
    mask_in = pd.Series(True, index=df.index)
    if start:
        try:
            mask_in = mask_in & (dates >= pd.Timestamp(start))
        except Exception:
            pass
    if end:
        try:
            mask_in = mask_in & (dates <= pd.Timestamp(end))
        except Exception:
            pass
    return df[mask_no_date | mask_in].copy()


# ── Geographic filter ──────────────────────────────────────────────────────────

def filter_geo(df: pd.DataFrame, province=None, district=None, hospital=None) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    if province and "province" in df.columns:
        df = df[df["province"] == province]
    if district and "district" in df.columns:
        df = df[df["district"] == district]
    if hospital and "district_hospital" in df.columns:
        df = df[df["district_hospital"] == hospital]
    return df.copy() if not df.empty else df


# ── Dropdown option builders ───────────────────────────────────────────────────

def province_options(df: pd.DataFrame) -> list[dict]:
    if df is None or df.empty or "province" not in df.columns:
        return []
    return [{"label": v, "value": v}
            for v in sorted(df["province"].dropna().unique().tolist())]


def district_options(df: pd.DataFrame, province=None) -> list[dict]:
    if df is None or df.empty or "district" not in df.columns:
        return []
    if province and "province" in df.columns:
        df = df[df["province"] == province]
    return [{"label": v, "value": v}
            for v in sorted(df["district"].dropna().unique().tolist())]


def hospital_options(df: pd.DataFrame, district=None) -> list[dict]:
    if df is None or df.empty or "district_hospital" not in df.columns:
        return []
    if district and "district" in df.columns:
        df = df[df["district"] == district]
    return [{"label": v, "value": v}
            for v in sorted(df["district_hospital"].dropna().unique().tolist())]


# ── Unique children count ──────────────────────────────────────────────────────

def nuniq(df: pd.DataFrame) -> int:
    """
    Count unique children. Prefers 'entity_id' (true unique child ID from eTracker),
    falls back to 'tracked_entity_instance', then row count.
    This prevents double-counting children with multiple visits.
    """
    if df is None or df.empty:
        return 0
    for col in ("entity_id", TEI):
        if col in df.columns:
            return int(df[col].nunique())
    return len(df)


# ── Missed appointment detection ───────────────────────────────────────────────

def get_missed_appointments(df: pd.DataFrame,
                            threshold_days: int = 10) -> pd.DataFrame:
    """
    Return deduplicated rows where next_visit_date is more than threshold_days
    in the past. Deduplication uses entity_id so each child appears only once.

    A child is flagged as having a missed appointment when:
        today - next_visit_date > threshold_days

    Only children with a recorded next_visit_date are considered.
    """
    if df is None or df.empty or "next_visit_date" not in df.columns:
        return pd.DataFrame()

    today = pd.Timestamp.today().normalize()

    # Deduplicate to latest visit per child
    tei_col = "entity_id" if "entity_id" in df.columns else TEI
    if tei_col in df.columns and "immunization_date" in df.columns:
        latest = (
            df.sort_values("immunization_date", ascending=False)
              .drop_duplicates(subset=[tei_col])
              .copy()
        )
    else:
        latest = df.copy()

    # Compute days since scheduled next visit
    latest["next_visit_date"] = pd.to_datetime(latest["next_visit_date"], errors="coerce")
    latest["days_overdue"]    = (today - latest["next_visit_date"]).dt.days

    # Flag children whose visit is overdue by more than the threshold
    missed = latest[
        latest["next_visit_date"].notna() &
        (latest["days_overdue"] > threshold_days)
    ].copy()

    return missed.sort_values("days_overdue", ascending=False).reset_index(drop=True)
