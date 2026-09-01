"""
core/risk_classifier.py — Growth velocity-based risk classification.

For each child with ≥ 2 visits, compare consecutive measurements to compute:
  - weight_velocity  (kg/month)
  - height_velocity  (cm/month)

Age at each visit is derived from date_of_birth + immunization_date whenever both
are available, giving exact age in months rather than trusting the recorded field.

WHO growth velocity standards used
───────────────────────────────────
WHO Child Growth Standards (WHO Technical Report Series 924, 2009)
Weight velocity: WHO 3rd centile tables (minimum expected monthly gain)
Height velocity: derived from WHO incremental chart, 3rd centile
MUAC thresholds: < 11.5 cm = severe acute malnutrition (HIGH)
                 < 12.5 cm = moderate acute malnutrition (MEDIUM)

Risk rules
──────────
HIGH   — weight loss (velocity < 0) OR MUAC < 11.5 cm OR severe wasting
MEDIUM — weight velocity < 50% of age-expected minimum OR height velocity
          < 30% of expected OR MUAC 11.5–12.5 cm OR moderate wasting

Output: one row per at-risk child (latest visit) + velocity + risk columns.
"""
from __future__ import annotations

import warnings
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd


# ── WHO growth velocity reference values (3rd centile, kg/month & cm/month) ───
# Source: WHO Multicentre Growth Reference Study Group (2009)
# Breakpoints are the child's age in completed months at the START of the interval.

# (age_start_mo, age_end_mo): (min_weight_kg_per_mo, min_height_cm_per_mo)
_WHO_VEL: list[tuple[float, float, float, float]] = [
    (0,    3,   0.50, 3.00),   # 0–3 months
    (3,    6,   0.30, 2.00),   # 3–6 months
    (6,    9,   0.18, 1.50),   # 6–9 months
    (9,    12,  0.13, 1.20),   # 9–12 months
    (12,   18,  0.09, 0.80),   # 12–18 months
    (18,   24,  0.07, 0.65),   # 18–24 months
    (24,   36,  0.05, 0.45),   # 24–36 months
    (36,   60,  0.04, 0.35),   # 36–60 months
    (60,   999, 0.03, 0.25),   # 5 years +
]

_MUAC_HIGH   = 11.5   # cm  — severe acute malnutrition
_MUAC_MEDIUM = 12.5   # cm  — moderate acute malnutrition

# Velocity thresholds as fraction of WHO minimum that trigger a risk flag
_HIGH_WEIGHT_FRACTION   = 0.0    # < 0 × expected → HIGH  (i.e., actual weight loss)
_MEDIUM_WEIGHT_FRACTION = 0.50   # < 50% of expected → MEDIUM
_MEDIUM_HEIGHT_FRACTION = 0.30   # < 30% of expected height gain → MEDIUM


def _who_norms(age_months: float) -> tuple[float, float]:
    """Return (min_weight_kg_per_mo, min_height_cm_per_mo) for a given age.

    Not called on the hot path (see _who_norms_vectorized below) — kept as
    the scalar reference definition the vectorised version must match.
    """
    for a_start, a_end, w_min, h_min in _WHO_VEL:
        if a_start <= age_months < a_end:
            return w_min, h_min
    return _WHO_VEL[-1][2], _WHO_VEL[-1][3]   # oldest bracket


# Bin edges / lookup tables for the vectorised equivalent of _who_norms(),
# used by _who_norms_vectorized() below — same brackets, same fallback.
_WHO_BIN_EDGES = [b[0] for b in _WHO_VEL] + [_WHO_VEL[-1][1]]
_WHO_WMIN      = np.array([b[2] for b in _WHO_VEL])
_WHO_HMIN      = np.array([b[3] for b in _WHO_VEL])


def _who_norms_vectorized(age: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Vectorised equivalent of _who_norms() applied to a whole Series at once.

    Same bracket logic (a_start <= age < a_end), same last-bracket fallback
    for out-of-range values. Values are meaningless where age is NaN, but
    callers already guard on age.notna() before using them (matching the
    original row-wise function, which was simply never called for NaN age).
    """
    idx = pd.cut(age.fillna(-1), bins=_WHO_BIN_EDGES, labels=False,
                 right=False, include_lowest=True)
    idx = idx.fillna(len(_WHO_VEL) - 1).astype(int).clip(0, len(_WHO_VEL) - 1)
    w_min = pd.Series(_WHO_WMIN[idx.to_numpy()], index=age.index)
    h_min = pd.Series(_WHO_HMIN[idx.to_numpy()], index=age.index)
    return w_min, h_min


def _age_from_dob(
    dob: Optional[pd.Timestamp],
    visit_date: Optional[pd.Timestamp],
) -> Optional[float]:
    """Return exact age in months from date_of_birth and visit date.

    Not called on the hot path (see _age_from_dob_vectorized below) — kept
    as the scalar reference definition the vectorised version must match.
    """
    if pd.isna(dob) or pd.isna(visit_date):
        return None
    days = (visit_date - dob).days
    if days < 0:
        return None      # data entry error — visit before birth
    return days / 30.4375


def _age_from_dob_vectorized(dob: pd.Series, visit_date: pd.Series) -> pd.Series:
    """Vectorised equivalent of _age_from_dob() applied to whole Series at once."""
    days = (visit_date - dob).dt.days
    age = days / 30.4375
    return age.where(days >= 0)   # NaN where visit-before-birth or either side NaT


def _classify_row(row) -> str:
    """Per-visit risk classification for a single row.

    Not called on the hot path — classify_at_risk() below uses the vectorised
    version of this exact logic (np.select over high_mask/medium_mask). Kept
    here as the scalar reference definition the vectorised version must match.
    """
    age   = row["_age"]
    w_vel = row["weight_velocity"]
    h_vel = row["height_velocity"]
    muac  = row["_muac"]
    wast  = str(row.get("wasting_status", "") or "").lower()

    # ── HIGH ──────────────────────────────────────────────────────────────
    if not pd.isna(muac) and muac < _MUAC_HIGH:
        return "HIGH"
    if "severe" in wast:
        return "HIGH"
    if not pd.isna(w_vel) and w_vel < 0:
        return "HIGH"   # actual weight loss

    # ── WHO velocity comparison ───────────────────────────────────────────
    if not pd.isna(age):
        w_min, h_min = _who_norms(age)
        if not pd.isna(w_vel) and w_vel < (w_min * _MEDIUM_WEIGHT_FRACTION):
            return "MEDIUM"
        if not pd.isna(h_vel) and h_vel < (h_min * _MEDIUM_HEIGHT_FRACTION):
            return "MEDIUM"

    # ── MEDIUM (no age needed) ────────────────────────────────────────────
    if not pd.isna(muac) and muac < _MUAC_MEDIUM:
        return "MEDIUM"
    if "moderate" in wast or "wasted" in wast:
        return "MEDIUM"

    return "OK"


# ── MUAC parser ───────────────────────────────────────────────────────────────

def _parse_muac(series: pd.Series) -> pd.Series:
    """Extract numeric MUAC value from strings like '12.5 cm' or bare floats."""
    return pd.to_numeric(
        series.astype(str).str.extract(r"(\d+\.?\d*)")[0],
        errors="coerce",
    )


# ── Core classification ────────────────────────────────────────────────────────

def classify_at_risk(df: pd.DataFrame) -> pd.DataFrame:
    """
    Given a multi-visit DataFrame (all visits), return at-risk children
    (HIGH or MEDIUM) with one row per child using unique entity_id.

    Required columns: immunization_date, weight_at_visit_kg
    Preferred cols:   entity_id (unique child), height_at_visit_cm,
                      age_in_months, date_of_birth, muac_cm,
                      wasting_status, stunting_status, gender,
                      health_facility, district_hospital, district
    """
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # ── Resolve unique child ID column ────────────────────────────────────────
    # Prefer entity_id (true DHIS2 UID, no duplicates) over tracked_entity_instance
    tei_col = "tracked_entity_instance"
    if "entity_id" in df.columns:
        df[tei_col] = df["entity_id"].fillna(df.get(tei_col, pd.Series(dtype=str)))
    if tei_col not in df.columns or df[tei_col].isna().all():
        warnings.warn("[risk_classifier] No child ID column found — cannot deduplicate.")
        return pd.DataFrame()

    # ── Type coercions ────────────────────────────────────────────────────────
    df["immunization_date"]  = pd.to_datetime(df["immunization_date"],  errors="coerce")
    df["date_of_birth"]      = pd.to_datetime(df.get("date_of_birth",  pd.NaT),
                                               errors="coerce")
    df["weight_at_visit_kg"] = pd.to_numeric(df.get("weight_at_visit_kg"), errors="coerce")
    df["height_at_visit_cm"] = pd.to_numeric(df.get("height_at_visit_cm"), errors="coerce")
    df["age_in_months"]      = pd.to_numeric(df.get("age_in_months"),   errors="coerce")
    df["_muac"]              = (_parse_muac(df["muac_cm"])
                                 if "muac_cm" in df.columns
                                 else pd.Series(np.nan, index=df.index))

    # ── Compute exact age from DOB where available ─────────────────────────────
    dob_available = df["date_of_birth"].notna() & df["immunization_date"].notna()
    if dob_available.any():
        df["_exact_age"] = _age_from_dob_vectorized(
            df["date_of_birth"], df["immunization_date"]
        ).where(dob_available)
        # Use exact age where computable; fall back to recorded age_in_months
        df["_age"] = df["_exact_age"].fillna(df["age_in_months"])
    else:
        df["_age"] = df["age_in_months"]

    # ── Plausibility checks already applied in data._normalise(); ─────────────
    # enforce age range guard here too in case called with raw data
    df.loc[(df["_age"] < 0) | (df["_age"] > 84), "_age"] = np.nan
    df.loc[(df["weight_at_visit_kg"] < 1.5) | (df["weight_at_visit_kg"] > 35),
           "weight_at_visit_kg"] = np.nan
    df.loc[(df["height_at_visit_cm"] < 40) | (df["height_at_visit_cm"] > 130),
           "height_at_visit_cm"] = np.nan

    # Drop rows with no weight or no date
    df = df.dropna(subset=["immunization_date", "weight_at_visit_kg"])
    if df.empty:
        return pd.DataFrame()

    # ── Sort by child + visit date ────────────────────────────────────────────
    df = df.sort_values([tei_col, "immunization_date"]).reset_index(drop=True)

    # ── Compute inter-visit velocity ──────────────────────────────────────────
    g = df.groupby(tei_col)
    df["_w_prev"] = g["weight_at_visit_kg"].shift(1)
    df["_h_prev"] = g["height_at_visit_cm"].shift(1)
    df["_d_prev"] = g["immunization_date"].shift(1)
    df["_days"]   = (df["immunization_date"] - df["_d_prev"]).dt.days.clip(lower=1)
    df["_months"] = df["_days"] / 30.4375

    df["weight_velocity"] = (df["weight_at_visit_kg"] - df["_w_prev"]) / df["_months"]
    df["height_velocity"] = (df["height_at_visit_cm"] - df["_h_prev"]) / df["_months"]

    # First visit per child has no previous → NaN velocity (no flag from velocity alone)
    df.loc[df["_w_prev"].isna(), ["weight_velocity", "height_velocity"]] = np.nan

    # ── Velocity plausibility clamp ───────────────────────────────────────────
    # Velocities outside physiological bounds are likely data errors, not true growth
    df.loc[df["weight_velocity"].abs() > 3.0,  "weight_velocity"] = np.nan
    df.loc[df["height_velocity"].abs() > 10.0, "height_velocity"] = np.nan

    # ── Per-visit risk classification (vectorised) ────────────────────────────
    # Same precedence as the row-wise version this replaces: HIGH conditions
    # checked first (muac<11.5 OR "severe" in wasting_status OR weight loss),
    # then age-dependent MEDIUM velocity checks, then MEDIUM muac/wasting —
    # np.select() picks the first true condition per row, matching the
    # original if/elif early-return order exactly.
    muac = df["_muac"]
    w_vel = df["weight_velocity"]
    h_vel = df["height_velocity"]
    age   = df["_age"]
    wast  = (df["wasting_status"] if "wasting_status" in df.columns
             else pd.Series("", index=df.index)).fillna("").astype(str).str.lower()

    who_w_min, who_h_min = _who_norms_vectorized(age)

    high_mask = (
        (muac.notna() & (muac < _MUAC_HIGH)) |
        wast.str.contains("severe", na=False) |
        (w_vel.notna() & (w_vel < 0))
    )
    medium_velocity_mask = age.notna() & (
        (w_vel.notna() & (w_vel < who_w_min * _MEDIUM_WEIGHT_FRACTION)) |
        (h_vel.notna() & (h_vel < who_h_min * _MEDIUM_HEIGHT_FRACTION))
    )
    medium_other_mask = (
        (muac.notna() & (muac < _MUAC_MEDIUM)) |
        wast.str.contains("moderate", na=False) |
        wast.str.contains("wasted", na=False)
    )
    medium_mask = medium_velocity_mask | medium_other_mask

    df["risk_level"] = np.select([high_mask, medium_mask], ["HIGH", "MEDIUM"], default="OK")

    # ── Aggregate to one row per child (worst risk ever seen) ─────────────────
    risk_order = {"HIGH": 3, "MEDIUM": 2, "OK": 1}
    df["_risk_score"] = df["risk_level"].map(risk_order).fillna(0)

    worst_risk = (
        df.groupby(tei_col)["_risk_score"]
        .max()
        .rename("_worst_score")
        .reset_index()
    )

    visit_counts = df.groupby(tei_col).size().rename("visit_count").reset_index()

    # Take the LATEST visit row per child
    latest = (
        df.sort_values("immunization_date", ascending=False)
        .drop_duplicates(subset=[tei_col])
        .merge(worst_risk,   on=tei_col, how="left")
        .merge(visit_counts, on=tei_col, how="left")
    )

    inv_risk = {v: k for k, v in risk_order.items()}
    latest["risk_level"]          = latest["_worst_score"].map(inv_risk).fillna("OK")
    latest["velocity_available"]  = latest["visit_count"] >= 2
    latest["age_in_months"]       = latest["_age"]   # expose computed/exact age

    # Keep only at-risk rows
    at_risk = latest[latest["risk_level"].isin(["HIGH", "MEDIUM"])].copy()

    # Clean internal columns
    drop_cols = [c for c in at_risk.columns if c.startswith("_")]
    at_risk   = at_risk.drop(columns=drop_cols, errors="ignore")

    # Sort: HIGH first, worst weight velocity first within each level
    at_risk["_sort"] = at_risk["risk_level"].map({"HIGH": 0, "MEDIUM": 1})
    at_risk = (at_risk
               .sort_values(["_sort", "weight_velocity"], na_position="last")
               .drop(columns="_sort"))

    return at_risk.reset_index(drop=True)


# ── Vaccination coverage & missed-dose flagging ───────────────────────────────
# Rwanda EPI schedule (UNEPI / Rwanda MOH):
# Each entry: (vaccine_column, schedule_name, due_age_months_min, due_age_months_max)
# Due window: child should have received vaccine if their current age >= due_age_months_min.
# We flag as MISSED if age ≥ due_age_months_max and the recorded value is not "Yes"/"1"/truthy.

_EPI_SCHEDULE: list[tuple[str, str, float, float]] = [
    ("bcg",             "BCG",              0,    2),
    ("opv",             "OPV0 (at birth)",  0,    2),
    ("hep_b_birth",     "HepB (at birth)",  0,    2),
    ("dpt_hepb_hib",    "DPT-HepB-Hib 1",  6,    8),
    ("pneumococcal",    "PCV 1",            6,    8),
    ("rotavirus",       "Rota 1",           6,    8),
    ("dpt_hepb_hib",    "DPT-HepB-Hib 2",  10,   12),
    ("pneumococcal",    "PCV 2",            10,   12),
    ("rotavirus",       "Rota 2",           10,   12),
    ("dpt_hepb_hib",    "DPT-HepB-Hib 3",  14,   16),
    ("pneumococcal",    "PCV 3",            14,   16),
    ("measles_rubella", "MR 1",             9,    10),
    ("measles_rubella", "MR 2",             15,   18),
]


def _is_given(val) -> bool:
    """Return True if a vaccine value indicates it was administered."""
    if pd.isna(val):
        return False
    s = str(val).strip().lower()
    return s in {"yes", "1", "true", "given", "done", "administered"}


def flag_missed_vaccinations(df: pd.DataFrame) -> pd.DataFrame:
    """
    Vectorised: add 'missed_vaccines' (list), 'missed_count' (int), and
    'vaccination_complete' (bool) to df based on Rwanda EPI schedule and age.

    A vaccine is flagged as MISSED when:
      • the child's age >= due_age_months_max (past the expected window)
      • AND the recorded vaccine value is not truthy ("Yes", "1", etc.)

    Uses age from '_age' (DOB-derived) if available, else 'age_in_months'.
    Vectorised over schedule entries — no iterrows().
    """
    if df is None or df.empty:
        return df

    df     = df.copy()
    age_col = "_age" if "_age" in df.columns else "age_in_months"
    age    = pd.to_numeric(df[age_col], errors="coerce") if age_col in df.columns \
             else pd.Series(np.nan, index=df.index)

    # Build a per-row missed list using boolean masks, not iterrows
    missed_cols: dict[int, list[str]] = {i: [] for i in df.index}

    for col, vax_name, _due_min, due_max in _EPI_SCHEDULE:
        if col not in df.columns:
            continue
        past_window = age >= due_max                     # vectorised age check
        not_given   = ~df[col].apply(_is_given)         # vectorised given check
        for idx in df.index[past_window & not_given]:
            missed_cols[idx].append(vax_name)

    df["missed_vaccines"]      = [missed_cols[i] for i in df.index]
    df["missed_count"]         = df["missed_vaccines"].apply(len)
    df["vaccination_complete"] = df["missed_count"] == 0
    return df


def vaccination_coverage_summary(df: pd.DataFrame) -> pd.DataFrame:
    """
    Return a summary DataFrame with one row per vaccine antigen showing
    number given and coverage %.  Uses deduplicated latest visits per child
    (entity_id or tracked_entity_instance).
    """
    if df is None or df.empty:
        return pd.DataFrame()

    tei = "entity_id" if "entity_id" in df.columns else "tracked_entity_instance"
    vax_cols = [c for c in ("bcg", "opv", "hep_b_birth", "dpt_hepb_hib",
                             "pneumococcal", "rotavirus", "measles_rubella", "hpv")
                if c in df.columns]
    if not vax_cols:
        return pd.DataFrame()

    # Latest visit per child (deduplication by entity_id)
    latest = (
        df.sort_values("immunization_date", ascending=False)
        .drop_duplicates(subset=[tei])
        if tei in df.columns
        else df
    )

    total = len(latest)
    rows = []
    labels = {
        "bcg": "BCG", "opv": "OPV", "hep_b_birth": "HepB (birth)",
        "dpt_hepb_hib": "DPT-HepB-Hib", "pneumococcal": "PCV",
        "rotavirus": "Rotavirus", "measles_rubella": "MR", "hpv": "HPV",
    }
    for col in vax_cols:
        n_given = latest[col].apply(_is_given).sum()
        rows.append({
            "vaccine":   labels.get(col, col.upper()),
            "given":     int(n_given),
            "total":     total,
            "coverage":  round(n_given / max(total, 1) * 100, 1),
        })

    return pd.DataFrame(rows)


# ── Dashboard convenience wrapper ─────────────────────────────────────────────

def build_risk_df_for_dashboard(df: pd.DataFrame,
                                district: str | None = None,
                                hospital: str | None = None) -> pd.DataFrame:
    """
    Given the stunted DataFrame from data.get_df(), try to load full visit
    history from local DB for accurate velocity computation, restricted to the
    selected district/hospital. Falls back to classifying the supplied DF if the
    DB is unreachable.
    """
    tei = "tracked_entity_instance"

    # Check how many unique children have >1 visit in the supplied DF
    if tei in df.columns:
        multi_pct = (df.groupby(tei).size() > 1).mean()
    else:
        multi_pct = 0.0

    if multi_pct >= 0.05:
        return classify_at_risk(df)

    # Load all visits from local DB (not just stunted) for better velocity.
    # NOTE: the district expression MUST match data.get_df() so the dropdown
    # values line up — otherwise the filter appears to do nothing.
    _DIST = "COALESCE(h_district, residence_district, district_source)"
    try:
        from config.db_local import get_local_conn
        conn = get_local_conn()
        sql  = f"""
            SELECT
                entity_id,
                entity_id                   AS tracked_entity_instance,
                child_name,
                date_of_birth,
                gender,
                last_immunization_date      AS immunization_date,
                age_visit_months            AS age_in_months,
                weight_visit_kg             AS weight_at_visit_kg,
                height_visit_cm             AS height_at_visit_cm,
                muac_cm,
                stunting_status,
                wasting_status,
                health_facility,
                h_district_hospital         AS district_hospital,
                {_DIST}                     AS district,
                mother_names, mother_phone,
                father_names, father_phone,
                bcg, opv, hep_b_birth, dpt_hepb_hib,
                pneumococcal, rotavirus, measles_rubella, hpv
            FROM immunization_vaccination
            WHERE entity_id IS NOT NULL
              AND last_immunization_date IS NOT NULL
              AND weight_visit_kg IS NOT NULL
        """
        params: list = []
        if district:
            sql += f" AND {_DIST} = %s"
            params.append(district)
        if hospital:
            sql += " AND h_district_hospital = %s"
            params.append(hospital)
        cur  = conn.cursor()
        cur.execute(sql, params or None)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        cur.close()
        conn.close()
        all_visits = pd.DataFrame(rows, columns=cols)
        return classify_at_risk(all_visits)
    except Exception:
        return classify_at_risk(df)


def build_risk_df_for_period(stunted_df: pd.DataFrame) -> pd.DataFrame:
    """
    At-risk (growth-velocity) classification scoped to a specific reporting
    period's stunted cohort — NOT all-time, and NOT all vaccinated children.

    Matches how the At-Risk dashboard page frames the question ("of children
    who are stunted, which ones are getting worse?"), scoped down further to
    just the children stunted in this reporting month — e.g. "July: 30,000
    vaccinated, 5,000 stunted, ~3,000 at-risk" rather than an all-time,
    all-children figure that isn't comparable to the rest of a monthly report.

    Velocity needs each child's full visit history (not just their one row in
    stunted_df), so this loads that history from the DB for just these
    children's IDs — not a full-table scan like build_risk_df_for_dashboard's
    "no filter" fallback.
    """
    tei_col = ("tracked_entity_instance" if "tracked_entity_instance" in stunted_df.columns
               else "entity_id" if "entity_id" in stunted_df.columns else None)
    if tei_col is None:
        return pd.DataFrame()
    ids = [i for i in stunted_df[tei_col].dropna().unique().tolist() if i]
    if not ids:
        return pd.DataFrame()

    _DIST = "COALESCE(h_district, residence_district, district_source)"
    try:
        from config.db_local import get_local_conn
        conn = get_local_conn()
        sql = f"""
            SELECT
                entity_id,
                entity_id                   AS tracked_entity_instance,
                child_name,
                date_of_birth,
                gender,
                last_immunization_date      AS immunization_date,
                age_visit_months            AS age_in_months,
                weight_visit_kg             AS weight_at_visit_kg,
                height_visit_cm             AS height_at_visit_cm,
                muac_cm,
                stunting_status,
                wasting_status,
                health_facility,
                h_district_hospital         AS district_hospital,
                {_DIST}                     AS district,
                mother_names, mother_phone,
                father_names, father_phone,
                bcg, opv, hep_b_birth, dpt_hepb_hib,
                pneumococcal, rotavirus, measles_rubella, hpv
            FROM immunization_vaccination
            WHERE entity_id = ANY(%s)
        """
        cur = conn.cursor()
        cur.execute(sql, (ids,))
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        cur.close()
        conn.close()
        visits = pd.DataFrame(rows, columns=cols)
        return classify_at_risk(visits)
    except Exception:
        return pd.DataFrame()


# ── Summary helpers ───────────────────────────────────────────────────────────

def get_risk_summary(at_risk_df: pd.DataFrame) -> dict:
    """Return high-level counts for the at-risk DataFrame."""
    if at_risk_df is None or at_risk_df.empty:
        return {"total": 0, "high": 0, "medium": 0,
                "districts": 0, "hospitals": 0, "facilities": 0}
    tei = ("entity_id" if "entity_id" in at_risk_df.columns
           else "tracked_entity_instance")
    return {
        "total":     int(at_risk_df[tei].nunique()) if tei in at_risk_df.columns
                     else len(at_risk_df),
        "high":      int((at_risk_df["risk_level"] == "HIGH").sum()),
        "medium":    int((at_risk_df["risk_level"] == "MEDIUM").sum()),
        "districts": (int(at_risk_df["district"].nunique())
                      if "district" in at_risk_df.columns else 0),
        "hospitals": (int(at_risk_df["district_hospital"].nunique())
                      if "district_hospital" in at_risk_df.columns else 0),
        "facilities": (int(at_risk_df["health_facility"].nunique())
                       if "health_facility" in at_risk_df.columns else 0),
    }
