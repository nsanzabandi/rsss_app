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
import warnings

# Suppress pandas FutureWarnings and SQLAlchemy warnings
pd.set_option('future.no_silent_downcasting', True)
warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', '.*pandas only supports SQLAlchemy.*')

# ── Constants ──────────────────────────────────────────────────────────────────

TEI        = "tracked_entity_instance"
_CSV_PATH  = Path(__file__).parent / "data" / "combined_df.csv"
_CACHE_TTL = 3600   # 1 hour
# get_df()'s table only changes when an eTracker sync runs, and
# refresh_after_sync() reloads it then — so no need to re-read it hourly (each
# re-read is ~14s of heavy work that slows every visitor while it runs).
_DF_TTL = 24 * 3600

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
                .str.strip()
                .replace(["nan", "NaN", "None", "null", "none", ""], np.nan)
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

_df_reloading = threading.Event()


def _reload_df_background() -> None:
    if _df_reloading.is_set():
        return
    _df_reloading.set()

    def _work():
        try:
            df = _load_from_db()
            if df is None:
                df = _load_from_csv()
            if df is not None:
                with _lock:
                    _cache["df"], _cache["ts"] = df, time.time()
                    _cache["source"] = "postgresql" if len(df) > 0 else "csv"
        finally:
            _df_reloading.clear()
    threading.Thread(target=_work, daemon=True).start()


def get_df_if_ready() -> pd.DataFrame | None:
    """get_df()'s table if it's already in memory, else None — and start
    loading it in the background. Never blocks (the load takes ~14s)."""
    with _lock:
        df = _cache.get("df")
        stale = (time.time() - _cache.get("ts", 0)) >= _DF_TTL
    if df is None or stale:
        _reload_df_background()
    return df


def get_df() -> pd.DataFrame | None:
    """Return cached stunted-children DataFrame. When it's older than the TTL,
    the old copy is served while a fresh one loads in the background (the load
    takes ~14s) — only the very first call after startup has to wait."""
    with _lock:
        if "df" in _cache and _cache["df"] is not None:
            if (time.time() - _cache.get("ts", 0)) >= _DF_TTL:
                _reload_df_background()
            return _cache["df"]
    with _lock:
        if not ("df" in _cache and (time.time() - _cache.get("ts", 0)) < _DF_TTL):
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
        cur = conn.cursor(name="all_children_stream")   # server-side cursor
        cur.itersize = 200_000
        cur.execute(_SQL_ALL)
        frames, cols = [], None
        while True:
            batch = cur.fetchmany(200_000)
            if not batch:
                break
            # A server-side cursor only fills .description after the first fetch.
            cols = cols or [d[0] for d in cur.description]
            frames.append(pd.DataFrame(batch, columns=cols))
        cur.close(); conn.close()
        if not frames:
            print("[data] All-children query returned 0 rows — trying CSV.")
            return None
        df = pd.concat(frames, ignore_index=True)
        del frames
        df = _derive_severe(_normalise(df))
        for _c in ["province", "district", "district_hospital",
                   "health_facility", "sector", "stunting_status"]:
            if _c in df.columns:
                df[_c] = df[_c].astype("category")
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
    event_id,
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
    muac_cm, wasting_status,
    bcg, opv, hep_b_birth, dpt_hepb_hib,
    pneumococcal, rotavirus, measles_rubella, hpv,
    COALESCE(h_district, residence_district, district_source) AS district,
    h_district_hospital      AS district_hospital,
    health_facility,
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


def _load_measurements(ids: list[str] | None = None) -> pd.DataFrame | None:
    """All visits, or (ids given) the full visit history of just those children."""
    try:
        from config.db_local import get_local_conn
        conn = get_local_conn()
        cur = conn.cursor()
        if ids is None:
            cur.execute(_MEAS_SQL)
        else:
            cur.execute(_MEAS_SQL + " WHERE entity_id = ANY(%s)", (ids,))
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        cur.close(); conn.close()
        if not rows:
            raise ValueError("0 rows")
        df = pd.DataFrame(rows, columns=cols)
        print(f"[data] {len(df):,} raw measurement rows from PostgreSQL.")
        return df
    except Exception as exc:
        if ids is not None:
            print(f"[data] Measurements for changed children unavailable ({exc}).")
            return None
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

# ── Disk-backed cache (populated by tools/build_child_cache.py) ────────────────
# The heavy build (~2 min, CPU-bound) doesn't have to run inside the same
# process that serves the dashboard. tools/build_child_cache.py can run it on
# a schedule (cron/systemd timer) and write the result here; the app then just
# reads it — immune to system load, and survives app restarts (no more waiting
# out a fresh 2-minute build every time the process restarts). If no disk
# cache exists yet (fresh install, or the scheduled job hasn't run), the app
# falls back to building it in-process exactly as before — fully backward
# compatible.
_DISK_CACHE_DIR      = Path(__file__).parent / "data" / "cache"
_DISK_CACHE_MAX_AGE  = 2 * 3600   # accept a disk cache up to 2h old before giving up on it


def _disk_cache_paths() -> dict:
    return {
        "child":    _DISK_CACHE_DIR / "child.pkl",
        "monthly":  _DISK_CACHE_DIR / "monthly.pkl",
        "schedule": _DISK_CACHE_DIR / "schedule.pkl",
        "visits":   _DISK_CACHE_DIR / "visits.pkl",
        "risk":     _DISK_CACHE_DIR / "risk.pkl",
        "risk_monthly": _DISK_CACHE_DIR / "risk_monthly.pkl",
        "meta":     _DISK_CACHE_DIR / "meta.json",
    }


def _read_meta() -> dict:
    import json
    try:
        return json.loads(_disk_cache_paths()["meta"].read_text())
    except Exception:
        return {}


def _write_meta(meta: dict) -> None:
    import json, os
    path = _disk_cache_paths()["meta"]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta))
    os.replace(tmp, path)


def _date_sorted(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Sort once by visit date (+ tie-breaker) so period queries can take each
    child's latest row with drop_duplicates(keep="last") and no per-request
    sort — that sort was ~1s per dashboard refresh on 0.5M+ rows."""
    if df is None or df.empty:
        return df
    cols = [c for c in cols if c in df.columns]
    return df.sort_values(cols, kind="mergesort").reset_index(drop=True)


def save_child_cache_to_disk(child: pd.DataFrame, monthly: pd.DataFrame,
                             schedule: pd.DataFrame, visits: pd.DataFrame,
                             data_as_of: str | None = None,
                             risk: pd.DataFrame | None = None) -> None:
    """Persist the built tables to disk and update the in-memory cache.

    data_as_of: DB MAX(fetched_at) read before the build — the watermark the
    next incremental update (update_child_cache) diffs against.
    Each file is written to a temp name then renamed, so the app never reads
    a half-written pickle while an update is in progress.
    """
    import os
    _DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    paths = _disk_cache_paths()
    risk = risk if risk is not None else pd.DataFrame()
    visits = _date_sorted(visits, ["immunization_date", "event_id"])
    risk = _date_sorted(risk, ["immunization_date"])
    risk_monthly = _aggregate_risk(risk)
    for key, frame in (("child", child), ("monthly", monthly),
                       ("schedule", schedule), ("visits", visits),
                       ("risk", risk), ("risk_monthly", risk_monthly)):
        tmp = paths[key].with_suffix(".tmp")
        frame.to_pickle(tmp)
        os.replace(tmp, paths[key])
    built_at = time.time()
    _write_meta({"built_at": built_at, "n_children": len(child), "data_as_of": data_as_of,
                 "format": _CACHE_FORMAT})
    with _child_lock:
        _child_cache["child"]    = child
        _child_cache["monthly"]  = monthly
        _child_cache["schedule"] = schedule
        _child_cache["visits"]   = visits
        _child_cache["risk"]     = risk
        _child_cache["risk_monthly"] = risk_monthly
        _child_cache["ts"]       = built_at
        _child_state["status"]   = "done"
    if not risk.empty:
        _risk_latest(risk)
    _prewarm_default_views()


def _refresh_from_disk_if_newer() -> bool:
    """If a disk cache exists and is newer than what's in memory, load it.

    Cheap when there's nothing new to do (just a JSON timestamp read).
    Returns True if the in-memory cache was (re)loaded from disk.
    """
    import json
    paths = _disk_cache_paths()
    if not paths["meta"].exists():
        return False
    try:
        meta = json.loads(paths["meta"].read_text())
        built_at = float(meta.get("built_at", 0))
    except Exception:
        return False
    if (time.time() - built_at) > _DISK_CACHE_MAX_AGE:
        return False   # too stale to trust — caller falls back to live rebuild
    with _child_lock:
        if _child_cache.get("ts", 0) >= built_at:
            return False   # already have this version (or newer) in memory
    try:
        child    = pd.read_pickle(paths["child"])
        monthly  = pd.read_pickle(paths["monthly"])
        schedule = pd.read_pickle(paths["schedule"])
        visits   = pd.read_pickle(paths["visits"])
        risk     = pd.read_pickle(paths["risk"]) if paths["risk"].exists() else pd.DataFrame()
        risk_monthly = (pd.read_pickle(paths["risk_monthly"])
                        if paths["risk_monthly"].exists() else pd.DataFrame())
        if not visits["immunization_date"].is_monotonic_increasing:
            visits = _date_sorted(visits, ["immunization_date", "event_id"])
        if not risk.empty and not risk["immunization_date"].is_monotonic_increasing:
            risk = _date_sorted(risk, ["immunization_date"])
    except Exception as exc:
        print(f"[data] disk cache read failed: {exc}")
        return False
    with _child_lock:
        _child_cache["child"]    = child
        _child_cache["monthly"]  = monthly
        _child_cache["schedule"] = schedule
        _child_cache["visits"]   = visits
        _child_cache["risk"]     = risk
        _child_cache["risk_monthly"] = risk_monthly
        _child_cache["ts"]       = built_at
        _child_state["status"]   = "done"
    if not risk.empty:
        _risk_latest(risk)          # pre-collapse so the first at-risk view is instant
    _prewarm_default_views()
    print(f"[data] Loaded child-level cache from disk ({len(child):,} children, "
          f"built {(time.time()-built_at)/60:.0f} min ago).")
    return True


def _build_child_and_monthly(
    meas: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    from core.stunting_calculator import add_computed_stunting, _resolve

    v = add_computed_stunting(meas)
    rc = _resolve(v)
    tei, date = rc["tei"], rc["date"]
    v["_o"] = pd.to_datetime(v[date], errors="coerce") if date else pd.NaT
    v["_stunted"] = v["haz_calc"] < -2
    v["_severe"]  = v["haz_calc"] < -3

    # health_facility included so filter_by_user's health_center-role branch
    # (which matches on this column) actually has something to filter on here.
    geo = [c for c in ("province", "district", "district_hospital", "health_facility")
          if c in v.columns]

    # Secondary sort key so ties on the same visit date resolve the same way
    # every time — SQL doesn't guarantee row order, so without this, which
    # tied row survives drop_duplicates(keep="last") (and therefore the
    # final stunted count) could vary between identical queries.
    _sort_cols = ["_o", "event_id"] if "event_id" in v.columns else ["_o"]

    # ── one row per child = latest visit overall (keeps geo even if HAZ null) ──
    latest_all = (v.sort_values(_sort_cols).drop_duplicates(subset=[tei], keep="last")).copy()

    # current status from latest VALID (HAZ-computable) visit
    valid = v[v["haz_calc"].notna()]
    latest_valid = (valid.sort_values(_sort_cols)
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

    # Growth risk (early warning) per visit; each child's CURRENT risk is the
    # one at their latest weighed visit — a child who recovered isn't flagged.
    risk = pd.DataFrame()
    child["risk_level"], child["risk_flags"], child["under_risk"] = "OK", 0, False
    try:
        risk = _build_risk_visits(meas, valid)
        if not risk.empty:
            lr = (risk.sort_values("immunization_date", kind="mergesort")
                      .drop_duplicates(TEI, keep="last").set_index(TEI))
            child["risk_level"] = lr["risk_level"].astype(object).reindex(child.index).fillna("OK")
            child["risk_flags"] = lr["risk_flags"].reindex(child.index).fillna(0).astype("int16")
            child["under_risk"] = child["risk_level"].isin(["HIGH", "MEDIUM"])
    except Exception as exc:
        print(f"[data] growth-risk skipped: {exc}")

    child["sex"] = _sex_label(base[rc["sex"]]) if rc.get("sex") else None
    child = child.reset_index().rename(columns={tei: "tracked_entity_instance"})

    # ── slim visit-level table for PERIOD-scoped queries ──────────────────────
    # `child` collapses each child to their single latest visit EVER, so
    # filtering it by date only keeps children whose absolute latest visit
    # happens to fall in the selected window — wrong for "how many children
    # were found stunted during July," which needs to look at each child's
    # latest visit WITHIN July, not their latest visit overall. Keep the raw
    # HAZ-computable visits (slimmed to just what period summaries need) so
    # build_period_child_df() can answer that correctly, cheaply, without
    # re-running add_computed_stunting().
    # health_facility + immunization_schedule are kept so the monthly and
    # schedule aggregates can be rebuilt from this table alone — which is
    # what lets update_child_cache() refresh them without a full rebuild.
    visits_cols = [c for c in ("province", "district", "district_hospital",
                               "health_facility", "immunization_schedule")
                   if c in valid.columns]
    if "event_id" in valid.columns:
        visits_cols = ["event_id"] + visits_cols
    visits = valid[[tei] + visits_cols].copy()
    visits["immunization_date"] = valid["_o"]
    visits["is_stunted"] = valid["haz_calc"] < -2
    visits["is_severe"]  = valid["haz_calc"] < -3
    visits["sex"] = _sex_label(valid[rc["sex"]]).to_numpy() if rc.get("sex") else None
    visits = _compact(visits.rename(columns={tei: TEI}))

    monthly, schedule = _aggregate_visits(visits)
    return child, monthly, schedule, visits, risk


def _build_risk_visits(meas: pd.DataFrame, valid: pd.DataFrame) -> pd.DataFrame:
    """Slim per-visit growth-risk table: one row per weighed visit with risk
    level, reason flags (core.risk_classifier.REASONS), weight velocity, age,
    area, and whether that same visit measured the child as stunted."""
    from core.risk_classifier import classify_visits
    r = classify_visits(meas)
    if r is None or r.empty:
        return pd.DataFrame()
    out = pd.DataFrame({TEI: r[TEI].to_numpy(),
                        "immunization_date": pd.to_datetime(r["immunization_date"]).to_numpy()})
    for c in ("province", "district", "district_hospital", "health_facility"):
        if c in r.columns:
            out[c] = r[c].to_numpy()
    out["risk_level"]      = pd.Categorical(r["risk_level"].to_numpy(), categories=["OK", "MEDIUM", "HIGH"])
    out["risk_flags"]      = r["risk_flags"].to_numpy().astype("int16")
    out["weight_velocity"] = pd.to_numeric(r["weight_velocity"], errors="coerce").to_numpy().astype("float32")
    out["age_months"]      = pd.to_numeric(r["_age"], errors="coerce").to_numpy().astype("float32")
    if "event_id" in r.columns and "event_id" in valid.columns:
        stunted = valid.drop_duplicates("event_id").set_index("event_id")["_stunted"]
        out["is_stunted"] = r["event_id"].map(stunted).to_numpy()
    return _compact(out)


def _aggregate_risk(risk: pd.DataFrame) -> pd.DataFrame:
    """Month × area at-risk counts (each child's latest weighed visit that
    month) for the at-risk trend chart."""
    if risk is None or risk.empty:
        return pd.DataFrame()
    geo = [c for c in ("province", "district", "district_hospital", "health_facility")
           if c in risk.columns]
    today = pd.Timestamp.today().normalize()
    rv = risk[risk["immunization_date"].notna() & (risk["immunization_date"] <= today)].copy()
    rv["month"] = rv["immunization_date"].dt.to_period("M").dt.to_timestamp()
    rv = (rv.sort_values("immunization_date", kind="mergesort")
            .drop_duplicates(subset=[TEI, "month"], keep="last"))
    rv["_high"] = rv["risk_level"].eq("HIGH")
    rv["_med"]  = rv["risk_level"].eq("MEDIUM")
    m = (rv.groupby(["month"] + geo, dropna=False, observed=True)
           .agg(measured=(TEI, "size"), high=("_high", "sum"), medium=("_med", "sum"))
           .reset_index())
    for c in geo:
        m[c] = m[c].astype(object)
    return m


# Repeated text columns in the visits table — stored as categoricals, which
# keeps ~3.5M rows from costing hundreds of MB of duplicate strings.
_VISIT_CAT_COLS = ("province", "district", "district_hospital",
                   "health_facility", "immunization_schedule", "sex")


def _sex_label(series: pd.Series) -> pd.Series:
    """'Male' / 'Female' / None — same token list as the WHO HAZ calculation
    (core/stunting_calculator._sex_code: English, French, Kinyarwanda)."""
    from core.stunting_calculator import _sex_code
    code = _sex_code(series)
    return pd.Series(np.where(code == 1, "Male", np.where(code == 2, "Female", None)),
                     index=series.index)


def _compact(visits: pd.DataFrame) -> pd.DataFrame:
    for c in _VISIT_CAT_COLS:
        if c in visits.columns and not isinstance(visits[c].dtype, pd.CategoricalDtype):
            visits[c] = visits[c].astype("category")
    return visits


def _aggregate_visits(visits: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Monthly trend + by-schedule tables, derived from the visits table."""
    geo = [c for c in ("province", "district", "district_hospital", "health_facility")
           if c in visits.columns]
    sort_cols = (["immunization_date", "event_id"] if "event_id" in visits.columns
                 else ["immunization_date"])

    # ── monthly × geo aggregates for the trend (computed stunting) ────────────
    # One row per child per month — their LATEST visit that month — the same
    # cohort rule the monthly reports use (core/data_loader.py), so a month's
    # trend point equals that month's report summary. Counting visits instead
    # double-counted children seen twice in a month, and grouping by geo
    # without dropna=False silently dropped every child with a blank
    # province/district/hospital/facility (~5% of children).
    today = pd.Timestamp.today().normalize()
    vv = visits[visits["immunization_date"].notna()
                & (visits["immunization_date"] <= today)].copy()
    vv["month"] = vv["immunization_date"].dt.to_period("M").dt.to_timestamp()
    vv = vv.sort_values(sort_cols).drop_duplicates(subset=[TEI, "month"], keep="last")
    monthly = (vv.groupby(["month"] + geo, dropna=False, observed=True)
                 .agg(measured=(TEI, "size"),
                      stunted=("is_stunted", "sum"),
                      severe=("is_severe", "sum"))
                 .reset_index())

    # ── stunting by immunization schedule (EPI visit) × geo ───────────────────
    schedule = pd.DataFrame()
    if "immunization_schedule" in visits.columns:
        sv = visits[visits["immunization_schedule"].notna()]
        schedule = (sv.groupby(["immunization_schedule"] + geo, observed=True)
                      .agg(measured=(TEI, "nunique"),
                           stunted=("is_stunted", "sum"),
                           severe=("is_severe", "sum"))
                      .reset_index())

    # Plain object columns downstream (dashboard filters/labels), not categoricals.
    for frame in (monthly, schedule):
        for c in frame.columns:
            if isinstance(frame[c].dtype, pd.CategoricalDtype):
                frame[c] = frame[c].astype(object)
    return monthly, schedule


def get_child_df() -> pd.DataFrame | None:
    _refresh_from_disk_if_newer()   # cheap; picks up a scheduled rebuild without waiting
    with _child_lock:
        if "child" in _child_cache and (time.time() - _child_cache.get("ts", 0)) < _DISK_CACHE_MAX_AGE:
            return _child_cache["child"]
    return None


def get_monthly_df() -> pd.DataFrame | None:
    _refresh_from_disk_if_newer()
    with _child_lock:
        if "monthly" in _child_cache and (time.time() - _child_cache.get("ts", 0)) < _DISK_CACHE_MAX_AGE:
            return _child_cache["monthly"]
    return None


def get_schedule_df() -> pd.DataFrame | None:
    _refresh_from_disk_if_newer()
    with _child_lock:
        if "schedule" in _child_cache and (time.time() - _child_cache.get("ts", 0)) < _DISK_CACHE_MAX_AGE:
            return _child_cache["schedule"]
    return None


def get_visits_df() -> pd.DataFrame | None:
    """Slim per-visit computed-stunting table (one row per HAZ-computable
    visit), used by build_period_child_df() for date-range-scoped KPIs."""
    _refresh_from_disk_if_newer()
    with _child_lock:
        if "visits" in _child_cache and (time.time() - _child_cache.get("ts", 0)) < _DISK_CACHE_MAX_AGE:
            return _child_cache["visits"]
    return None


def get_risk_df() -> pd.DataFrame | None:
    """Per-visit growth-risk table (see _build_risk_visits)."""
    _refresh_from_disk_if_newer()
    with _child_lock:
        if "risk" in _child_cache and (time.time() - _child_cache.get("ts", 0)) < _DISK_CACHE_MAX_AGE:
            return _child_cache["risk"]
    return None


def get_risk_monthly_df() -> pd.DataFrame | None:
    _refresh_from_disk_if_newer()
    with _child_lock:
        if "risk_monthly" in _child_cache and (time.time() - _child_cache.get("ts", 0)) < _DISK_CACHE_MAX_AGE:
            return _child_cache["risk_monthly"]
    return None


_risk_latest_memo: dict = {}


def _risk_latest(risk: pd.DataFrame) -> pd.DataFrame:
    """Each child's latest weighed visit, computed once per risk table."""
    key = id(risk)
    if _risk_latest_memo.get("key") != key:
        latest = (risk.sort_values("immunization_date", kind="mergesort")
                      .drop_duplicates(TEI, keep="last"))
        _risk_latest_memo.clear()
        _risk_latest_memo.update(key=key, df=latest)
    return _risk_latest_memo["df"]


def scoped_risk_df(risk: pd.DataFrame | None, user: dict, province=None, district=None,
                   hospital=None, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """One row per child: their growth risk at their LATEST weighed visit
    within the date window, limited to the user's area + dashboard filters."""
    if risk is None or risk.empty:
        return pd.DataFrame()
    if _covers_all(risk, start, end):
        # Whole data range: each child's latest visit overall, collapsed once
        # per cache refresh (same shortcut as scoped_child_df).
        latest = _risk_latest(risk)
        return filter_geo(filter_by_user(latest, user), province, district, hospital)
    r = filter_geo(filter_by_user(risk, user), province, district, hospital)
    if start:
        r = r[r["immunization_date"] >= pd.Timestamp(start)]
    if end:
        r = r[r["immunization_date"] <= pd.Timestamp(end)]
    if r.empty:
        return r
    return r.drop_duplicates(TEI, keep="last")      # risk table is date-sorted


def summarize_risk(r: pd.DataFrame) -> dict:
    from core.risk_classifier import REASONS
    if r is None or r.empty:
        return dict(measured=0, at_risk=0, high=0, medium=0, pct=0.0, stunted_at_risk=0,
                    reasons={})
    at = r[r["risk_level"].isin(["HIGH", "MEDIUM"])]
    flags = at["risk_flags"].to_numpy()
    return dict(
        measured=len(r), at_risk=len(at),
        high=int((at["risk_level"] == "HIGH").sum()),
        medium=int((at["risk_level"] == "MEDIUM").sum()),
        pct=round(len(at) / max(len(r), 1) * 100, 1),
        stunted_at_risk=int(at["is_stunted"].eq(True).sum()) if "is_stunted" in at.columns else 0,
        reasons={label: int(((flags & bit) > 0).sum()) for bit, label in REASONS.items()},
    )


def get_monthly_df_stale_ok() -> pd.DataFrame | None:
    """Like get_monthly_df(), but ignores the normal freshness ceiling.

    For report generation (e.g. the National Overview trend chart), where a
    slightly-old trend beats a missing one — unlike live dashboard KPIs,
    which must not silently show outdated counts. Falls back to reading the
    on-disk cache file directly if nothing usable is in memory yet.
    """
    _refresh_from_disk_if_newer()
    with _child_lock:
        if "monthly" in _child_cache:
            return _child_cache["monthly"]
    path = _disk_cache_paths()["monthly"]
    if path.exists():
        try:
            return pd.read_pickle(path)
        except Exception:
            return None
    return None


def child_build_status() -> str:
    with _child_lock:
        return _child_state["status"]


def warm_child_level() -> None:
    """Ensure the per-child table + monthly trend are available (idempotent).

    Checks the disk cache first (written by tools/build_child_cache.py on a
    schedule, or by a previous in-process build) — that's near-instant and
    immune to system load. Only falls back to a full in-process rebuild
    (~2 min, CPU-bound) if no usable disk cache exists, e.g. a fresh install
    that hasn't run the scheduled build yet.
    """
    if _refresh_from_disk_if_newer():
        return
    with _child_lock:
        if _child_state["status"] == "running":
            return
        if "child" in _child_cache and (time.time() - _child_cache.get("ts", 0)) < _CACHE_TTL:
            return
        _child_state["status"] = "running"

    def _worker():
        try:
            update_child_cache()   # incremental when a disk cache exists, else full
        except Exception as exc:
            import traceback; traceback.print_exc()
            with _child_lock:
                _child_state["status"] = "error"
            print(f"[data] Child-level build failed: {exc}")

    threading.Thread(target=_worker, daemon=True).start()


# ── Incremental refresh (after an eTracker sync) ───────────────────────────────
# A full rebuild recomputes WHO stunting for all ~3.8M visits (~2.5 min). After
# a sync, only children with new/edited rows can change, so we recompute just
# those children's full histories and merge them into the cached tables.

_FULL_REBUILD_SHARE = 0.4    # if this share of children changed, a full rebuild is cheaper
_CACHE_FORMAT       = 5      # bump when the cached tables' shape changes → forces one full rebuild
                             # (5: child sex, for the dashboard's boys/girls split)
                             # (4: per-visit growth-risk table for the at-risk dashboard)
                             # (3: health_facility added so health-centre scoping works)


def _db_data_version() -> str | None:
    """Newest fetched_at in the local DB — the incremental watermark."""
    try:
        from config.db_local import get_local_conn
        conn = get_local_conn(); cur = conn.cursor()
        cur.execute("SELECT MAX(fetched_at) FROM immunization_vaccination")
        v = cur.fetchone()[0]
        cur.close(); conn.close()
        return v.isoformat() if v else None
    except Exception:
        return None


def _changed_children(since: str) -> list[str] | None:
    """Children with any row fetched after `since` (10-min overlap so a sync
    transaction that started before the watermark was read isn't missed —
    re-processing a few extra children is harmless)."""
    try:
        from config.db_local import get_local_conn
        conn = get_local_conn(); cur = conn.cursor()
        cur.execute("SELECT DISTINCT entity_id FROM immunization_vaccination "
                    "WHERE fetched_at > %s::timestamp - INTERVAL '10 minutes' "
                    "AND entity_id IS NOT NULL", (since,))
        ids = [r[0] for r in cur.fetchall()]
        cur.close(); conn.close()
        return ids
    except Exception as exc:
        print(f"[data] could not list changed children: {exc}")
        return None


def _build(ids: list | None):
    """Load measurements (all, or these children's full histories) and build
    the cached tables — with the Polars build (core/fast_build.py, ~10× faster)
    when available, else the original pandas build."""
    try:
        from core import fast_build
    except ImportError:
        fast_build = None
    if fast_build is not None:
        try:
            meas = fast_build.load_measurements(ids)
            if meas is None:
                raise RuntimeError("no measurements available")
            return fast_build.build_tables(meas)
        except RuntimeError:
            raise
        except Exception as exc:                     # never let the fast path take the app down
            print(f"[data] fast build failed ({exc}) — using the pandas build")
    meas = _load_measurements(ids)
    if meas is None or meas.empty:
        raise RuntimeError("no measurements available")
    return _build_child_and_monthly(meas)


def update_child_cache(full: bool = False) -> str:
    """Bring the computed per-child cache up to date with the DB.

    Returns "full", "incremental" or "unchanged". Falls back to a full rebuild
    when there's no usable cache yet, the cache predates this format, or so
    many children changed that recomputing everything is cheaper.
    """
    version = _db_data_version()   # read BEFORE loading, so rows landing mid-build are picked up next time
    meta = _read_meta()
    paths = _disk_cache_paths()

    usable = bool(not full and meta.get("data_as_of") and meta.get("format") == _CACHE_FORMAT
                  and paths["child"].exists() and paths["visits"].exists())

    ids = None
    if usable:
        ids = ([] if version == meta["data_as_of"]          # DB unchanged — skip the query
               else _changed_children(meta["data_as_of"]))
        if ids is not None and len(ids) > _FULL_REBUILD_SHARE * max(meta.get("n_children", 0), 1):
            print(f"[data] {len(ids):,} children changed — full rebuild is cheaper")
            ids = None

    child = visits = None
    if ids:
        try:
            child  = pd.read_pickle(paths["child"])
            visits = pd.read_pickle(paths["visits"])
        except Exception as exc:
            print(f"[data] cached tables unreadable ({exc}) — full rebuild")
            ids = None

    t0 = time.time()
    if ids is None:
        child, monthly, schedule, visits, risk = _build(None)
        save_child_cache_to_disk(child, monthly, schedule, visits, data_as_of=version, risk=risk)
        print(f"[data] Child cache FULL rebuild: {len(child):,} children in {time.time()-t0:.0f}s")
        return "full"

    if not ids:
        # Nothing new — the cache is still accurate, just mark it fresh so the
        # 2h staleness check doesn't trigger a pointless rebuild.
        built_at = time.time()
        _write_meta({**meta, "built_at": built_at, "data_as_of": version or meta["data_as_of"]})
        with _child_lock:
            if "child" in _child_cache:
                _child_cache["ts"] = built_at
            _child_state["status"] = "done"
        print("[data] Child cache already up to date.")
        return "unchanged"

    c_new, _, _, v_new, r_new = _build(ids)
    ids_set = set(ids)
    child  = pd.concat([child[~child[TEI].isin(ids_set)], c_new], ignore_index=True)
    visits = _compact(pd.concat([visits[~visits[TEI].isin(ids_set)], v_new], ignore_index=True))
    risk_old = pd.read_pickle(paths["risk"]) if paths["risk"].exists() else pd.DataFrame()
    risk = _compact(pd.concat([risk_old[~risk_old[TEI].isin(ids_set)] if not risk_old.empty else risk_old,
                               r_new], ignore_index=True))
    if "risk_level" in risk.columns:
        risk["risk_level"] = pd.Categorical(risk["risk_level"].astype(object),
                                            categories=["OK", "MEDIUM", "HIGH"])
    monthly, schedule = _aggregate_visits(visits)
    save_child_cache_to_disk(child, monthly, schedule, visits, data_as_of=version, risk=risk)
    print(f"[data] Child cache INCREMENTAL update: {len(ids):,} changed children "
          f"in {time.time()-t0:.0f}s")
    return "incremental"


def refresh_after_sync() -> None:
    """Call after an eTracker sync: update every dashboard cache in the
    background. The old data keeps being served until each new table is
    ready, then it's swapped in — users never wait on a reload."""
    with _child_lock:
        if _child_state["status"] == "running":
            return
        _child_state["status"] = "running"

    def _worker():
        try:
            update_child_cache()
        except Exception as exc:
            import traceback; traceback.print_exc()
            with _child_lock:
                _child_state["status"] = "error"
            print(f"[data] post-sync cache update failed: {exc}")
        # Stunted-by-column rows (Risk/Reports pages): load a fresh copy
        # outside the lock, then swap. get_all_df() is deliberately NOT
        # pre-loaded — no page calls it, and it would pin ~0.5–2 GB for nothing.
        try:
            df = _load_from_db()
            if df is not None:
                with _lock:
                    _cache["df"], _cache["ts"], _cache["source"] = df, time.time(), "postgresql"
        except Exception as exc:
            print(f"[data] post-sync reload failed: {exc}")
        with _all_lock:
            _all_cache.clear()
        with _meas_lock:
            _meas_cache.clear()

    threading.Thread(target=_worker, daemon=True).start()


def monthly_rate(monthly: pd.DataFrame, start=None, end=None, value: str = "rate") -> pd.DataFrame:
    """Collapse the monthly×geo table (get_monthly_df()) to a national-or-
    already-geo-filtered monthly series, dropping future and thin
    (low-denominator) months that make the line spike. Shared by the
    dashboard's Stunting Rate Over Time chart and the national overview PDF's
    trend chart, so both tell the same story from the same numbers.

    value="rate"   → % of measured children who are stunted
    value="severe" → % of stunted children who are severely stunted
    """
    m = (monthly.groupby("month")
                .agg(measured=("measured", "sum"),
                     stunted=("stunted", "sum"),
                     severe=("severe", "sum"))
                .reset_index())
    if start:
        m = m[m["month"] >= pd.Timestamp(start)]
    if end:
        m = m[m["month"] <= pd.Timestamp(end)]
    if m.empty:
        return m
    # Drop sparse/partial months relative to a typical busy month, so the line
    # isn't dragged by the current partial month or future-dated stragglers.
    # Only for a wide/default view, though — an explicitly narrowed range
    # (e.g. the user picked a single month) should show exactly what's in
    # it, not get thinned out by a heuristic meant for a long history.
    narrow = False
    if start and end:
        try:
            narrow = (pd.Timestamp(end) - pd.Timestamp(start)).days < _NARROW_RANGE_DAYS
        except Exception:
            narrow = False
    if not narrow:
        busy = m["measured"].quantile(0.75)
        thresh = max(100, 0.30 * busy)
        m = m[m["measured"] >= thresh].copy()
    if value == "rate":
        m["y"] = (m["stunted"] / m["measured"] * 100).round(1)
    else:  # severe share of stunted
        m = m[m["stunted"] > 0]
        m["y"] = (m["severe"] / m["stunted"] * 100).round(1)
    return m


def build_period_child_df(visits: pd.DataFrame | None,
                          start: str | None, end: str | None) -> pd.DataFrame:
    """
    Scope the per-visit computed-stunting table to a date window, then dedupe
    to one row per child using their LATEST visit WITHIN that window — not
    their latest visit ever. This matches how the monthly hospital/facility/
    national reports define a period's stunting cohort (core/data_loader.py),
    so a "Total Stunted" KPI filtered to e.g. July reflects children actually
    seen and assessed in July, not just the (much smaller) set of children
    whose single most-recent-ever visit happens to fall in July.

    Output has the same shape summarize_children() expects (is_stunted,
    is_severe, ever_stunted columns), so it's a drop-in replacement for
    filter_by_date(get_child_df(), ...) wherever a date range is applied.
    """
    if visits is None or visits.empty:
        return pd.DataFrame()

    v = visits
    if start:
        try:
            v = v[v["immunization_date"] >= pd.Timestamp(start)]
        except Exception:
            pass
    if end:
        try:
            v = v[v["immunization_date"] <= pd.Timestamp(end)]
        except Exception:
            pass
    if v.empty:
        return pd.DataFrame()

    tei = "tracked_entity_instance"
    ever = v.groupby(tei, sort=False)["is_stunted"].max()
    # visits are kept sorted by (date, event_id) — see _date_sorted — so the
    # last row per child is their latest visit in the window; no sort needed.
    latest = v.drop_duplicates(subset=[tei], keep="last").set_index(tei)
    latest["ever_stunted"] = ever.reindex(latest.index)
    # missed-dose / at-risk flags are current-state concepts (computed from a
    # child's overall latest visit + full growth history), not period-scoped —
    # they aren't shown in the KPI cards that use this, so left False here.
    latest["missed"]      = False
    latest["under_risk"]  = False
    return latest.reset_index()


# A range this wide behaves identically whether a child is deduped by "latest
# visit ever" (cheap — the pre-collapsed child table) or "latest visit within
# the range" (build_period_child_df, correct in general but expensive — it
# re-derives from millions of visit rows). The two only diverge for genuinely
# narrow windows (e.g. one month), which is exactly what build_period_child_df
# exists for. Below this threshold we're clearly looking at a specific period;
# at or above it, treat it the same as "no filter" and use the cheap path —
# this matters because the dashboard's own default date range spans the
# entire dataset, so without this the expensive path would run on every
# single page load, not just when someone narrows to a specific month.
_NARROW_RANGE_DAYS = 180


def _covers_all(frame: pd.DataFrame | None, start, end) -> bool:
    """True when the date window includes every visit — only then may the
    collapsed latest-visit-ever tables stand in for a real period query.
    (Previously ANY window over 180 days took that shortcut, so "Year 2025"
    silently showed all-time figures.) Tables are date-sorted, so first/last
    rows are the date range."""
    if frame is None or frame.empty or (not start and not end):
        return True
    first, last = frame["immunization_date"].iloc[0], frame["immunization_date"].iloc[-1]
    last = min(last, pd.Timestamp.today().normalize())     # ignore future-dated typos
    try:
        return ((not start or pd.Timestamp(start) <= first) and
                (not end or pd.Timestamp(end) >= last))
    except Exception:
        return False


_scope_memo: dict = {}
_SCOPE_MEMO_MAX = 6                 # full per-child tables are large — keep a few
_scope_memo_lock = threading.Lock()
_scope_inflight: dict = {}          # key → Event while someone is computing it


def memo_scoped(kind: str, user: dict, province=None, district=None, hospital=None,
                start=None, end=None):
    """scoped_child_df / scoped_risk_df, remembered per (data version, user's
    area, filters, dates). Everyone opening the same view — e.g. every public
    visitor on the default national quarter — shares one computation; the
    memo resets whenever the cache is rebuilt."""
    ts = _child_cache.get("ts")
    role = user.get("role")
    area = {"district": user.get("district"), "hospital": user.get("hospital"),
            "health_center": user.get("health_center")}.get(role)
    scope = "national" if role in ("ministry", "public") else role   # same figures → share
    frame = get_visits_df() if kind == "child" else get_risk_df()
    if _covers_all(frame, start, end):
        start = end = None                    # any whole-data range is the same "All time"
    key = (kind, ts, scope, area, province, district, hospital, start, end)
    while True:
        with _scope_memo_lock:
            if key in _scope_memo:
                _scope_memo[key] = _scope_memo.pop(key)      # most recently used
                return _scope_memo[key]
            ev = _scope_inflight.get(key)
            if ev is None:                                   # nobody computing it → we do
                ev = _scope_inflight[key] = threading.Event()
                break
        ev.wait(timeout=120)          # someone else is computing this exact view — reuse it
    try:
        if kind == "child":
            out = scoped_child_df(get_child_df(), get_visits_df(), user, province, district,
                                  hospital, start, end)
        else:
            out = scoped_risk_df(get_risk_df(), user, province, district, hospital, start, end)
        with _scope_memo_lock:
            if any(k[1] != ts for k in _scope_memo):        # data changed → drop old entries
                _scope_memo.clear()
            _scope_memo[key] = out
            while len(_scope_memo) > _SCOPE_MEMO_MAX:
                _scope_memo.pop(next(iter(_scope_memo)))
        return out
    finally:
        with _scope_memo_lock:
            _scope_inflight.pop(key, None)
        ev.set()


def _prewarm_default_views() -> None:
    """In the background, compute what the dashboard opens on — last month vs
    the month before — then Last 3 months and All time, so the first visitor
    after a (re)load doesn't wait."""
    def _work():
        try:
            today = pd.Timestamp.today().normalize()
            ly = today - pd.DateOffset(years=1)
            user = {"role": "public"}
            # default view: last month, and the month before (▲/▼)
            m0 = today.replace(day=1)
            lm = m0.to_period("M") - 1
            for per in (lm, lm - 1):
                a, b = per.start_time, per.end_time.normalize()
                memo_summary("child", user, start=f"{a:%Y-%m-%d}", end=f"{b:%Y-%m-%d}")
                memo_summary("risk", user, start=f"{a:%Y-%m-%d}", end=f"{b:%Y-%m-%d}")
            # then Last 3 months vs the 3 before
            for k in (3, 6):
                a = (m0.to_period("M") - k).to_timestamp()
                b = (m0.to_period("M") - k + 3).to_timestamp() - pd.Timedelta(days=1)
                memo_summary("child", user, start=f"{a:%Y-%m-%d}", end=f"{b:%Y-%m-%d}")
                memo_summary("risk", user, start=f"{a:%Y-%m-%d}", end=f"{b:%Y-%m-%d}")
            memo_summary("child", user); memo_summary("risk", user)        # All time totals
            # the default view's ▲/▼: this year so far vs the same dates last year
            for a, b in ((f"{today.year}-01-01", f"{today:%Y-%m-%d}"),
                         (f"{ly.year}-01-01", f"{ly:%Y-%m-%d}")):
                memo_summary("child", user, start=a, end=b)
                memo_summary("risk", user, start=a, end=b)
        except Exception as exc:
            print(f"[data] pre-warm skipped: {exc}")
    threading.Thread(target=_work, daemon=True).start()


_summary_memo: dict = {}
_SUMMARY_MEMO_MAX = 400             # card figures are a few numbers — keep many


def memo_summary(kind: str, user: dict, province=None, district=None, hospital=None,
                 start=None, end=None) -> dict:
    """The KPI figures (summarize_children / summarize_risk) for a view,
    remembered per data version — switching back to a period seen before is
    instant, without keeping its full table in memory."""
    ts = _child_cache.get("ts")
    role = user.get("role")
    scope = "national" if role in ("ministry", "public") else role
    area = {"district": user.get("district"), "hospital": user.get("hospital"),
            "health_center": user.get("health_center")}.get(role)
    frame = get_visits_df() if kind == "child" else get_risk_df()
    if _covers_all(frame, start, end):
        start = end = None
    key = (kind, ts, scope, area, province, district, hospital, start, end)
    with _scope_memo_lock:
        if key in _summary_memo:
            return _summary_memo[key]
    df = memo_scoped(kind, user, province, district, hospital, start, end)
    out = summarize_children(df) if kind == "child" else summarize_risk(df)
    with _scope_memo_lock:
        if any(k[1] != ts for k in _summary_memo):
            _summary_memo.clear()
        _summary_memo[key] = out
        while len(_summary_memo) > _SUMMARY_MEMO_MAX:
            _summary_memo.pop(next(iter(_summary_memo)))
    return out


def scoped_child_df(child: pd.DataFrame | None, visits: pd.DataFrame | None,
                    user: dict, province=None, district=None, hospital=None,
                    start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """One-stop, correctly-and-cheaply-scoped per-child view for the dashboard.

    Uses the pre-collapsed child table (cheap) only when the window covers all
    the data (see _covers_all); any real window, short or long, goes through
    build_period_child_df(). Applies user/geo filters to whichever it picks.
    """
    if not _covers_all(visits, start, end) and visits is not None:
        v = filter_by_user(visits, user)
        v = filter_geo(v, province, district, hospital)
        return build_period_child_df(v, start, end)

    c = child if child is not None else pd.DataFrame()
    c = filter_by_user(c, user)
    c = filter_geo(c, province, district, hospital)
    return c


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

    if role in ("ministry", "public"):
        return df
    # Fail CLOSED: if the user's area can't be applied (scope value or column
    # missing, unknown role), return nothing — never the whole country. This
    # used to fall through to `return df`, so health-centre users saw national
    # data wherever a table had no health_facility column.
    col, val = {"district":      ("district", district),
                "hospital":      ("district_hospital", hospital),
                "health_center": ("health_facility", hc)}.get(role, (None, None))
    if not col or not val or col not in df.columns:
        return df.iloc[0:0].copy()
    return df[df[col] == val].copy()


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
    if df is None or df.empty or not (province or district or hospital):
        return df          # nothing to filter — no copy (tables can be 1M+ rows)
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
