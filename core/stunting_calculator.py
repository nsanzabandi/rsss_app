"""
core/stunting_calculator.py — Compute stunting ourselves from raw measurements,
instead of trusting the eTracker `stunting_status` column.

Method (WHO Child Growth Standards, height/length-for-age):
  • age (months) = (visit date − date of birth) / 30.4375   [falls back to the
    recorded age column when DOB or visit date is missing]
  • HAZ (height-for-age z-score) via the WHO LMS method:
        Z = ((height / M) ** L − 1) / (L · S)
    with sex-specific L, M, S from config/who_hfa_lms.csv (0–60 months), taken
    at the exact age (linear between months), as WHO Anthro does.
  • Classification (per visit):
        HAZ < −3  → Severe stunting
        −3 ≤ HAZ < −2 → Moderate stunting
        HAZ ≥ −2  → Normal
  • Only a height measured at that visit counts. Biologically implausible
    heights and |HAZ| > 6 are treated as errors (not measured), and so are
    copied or shrinking heights (core/fast_build.suspect_heights). A blank
    duplicate event on the same day takes that day's measured height; heights
    are never borrowed from the child's other visit days — an older height at
    an older age made the child look stunted. A visit with no usable height
    stays unclassified.

Child-level status = the most recent VALID visit (current status). An
`ever_stunted` flag is also produced for comparison.

Everything is deduplicated by the unique child id (entity_id / tracked_entity_instance).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

_LMS_PATH = Path(__file__).resolve().parent.parent / "config" / "who_hfa_lms.csv"
_lms_cache: pd.DataFrame | None = None

# Physiological height bounds (cm) for children 0–60 months — anything outside
# is a data-entry error, not a real measurement.
_HEIGHT_MIN, _HEIGHT_MAX = 38.0, 140.0
_HAZ_FLAG = 6.0   # |HAZ| beyond this is biologically implausible (WHO flag)

# Heights that can't be right next to the child's previous measured visit
# (also used by core/fast_build.suspect_heights):
COPY_MIN_DAYS = 28      # a baby can't keep the same length for 4 weeks …
COPY_MAX_AGE  = 6.0     # … while under 6 months old (~2–4 cm a month)
SHRINK_CM     = 2.0     # shorter than the previous visit by more than this

_MALE_TOKENS   = {"1", "m", "male", "boy", "garcon", "garçon", "umuhungu", "gabo"}
_FEMALE_TOKENS = {"2", "f", "female", "girl", "fille", "umukobwa", "gore"}


# ── Column resolution ──────────────────────────────────────────────────────────

def _first(df: pd.DataFrame, names: list[str]) -> str | None:
    for n in names:
        if n in df.columns:
            return n
    return None


def _resolve(df: pd.DataFrame) -> dict:
    return {
        "tei":    _first(df, ["entity_id", "tracked_entity_instance"]),
        "sex":    _first(df, ["gender", "sex"]),
        "dob":    _first(df, ["date_of_birth", "dob"]),
        "date":   _first(df, ["immunization_date", "last_immunization_date"]),
        "height": _first(df, ["height_at_visit_cm", "height_visit_cm", "height_cm"]),
        "age":    _first(df, ["age_in_months", "age_visit_months"]),
    }


def _sex_code(series: pd.Series) -> np.ndarray:
    s = series.astype(str).str.strip().str.lower()
    out = np.full(len(s), np.nan)
    out[s.isin(_MALE_TOKENS)]   = 1
    out[s.isin(_FEMALE_TOKENS)] = 2
    return out


# ── HAZ via WHO LMS ─────────────────────────────────────────────────────────────

def _load_lms() -> pd.DataFrame:
    global _lms_cache
    if _lms_cache is None:
        if not _LMS_PATH.exists():
            raise FileNotFoundError(
                f"WHO LMS table not found at {_LMS_PATH}. It ships with the repo; "
                "restore config/who_hfa_lms.csv."
            )
        _lms_cache = pd.read_csv(_LMS_PATH)
    return _lms_cache


def haz(height_cm, age_months, sex_code) -> np.ndarray:
    """Vectorized height-for-age z-score. Inputs are array-like, positionally
    aligned. Returns np.ndarray of HAZ (NaN where inputs are unusable).

    L, M, S are taken at the child's EXACT age (linear between the monthly
    table rows), as WHO Anthro does with its daily tables. Rounding the age
    to a whole month moved young babies onto the wrong reference — a
    3-week-old measured against the 1-month median looked stunted."""
    lms = _load_lms()
    age = np.clip(pd.to_numeric(pd.Series(age_months), errors="coerce").to_numpy(dtype=float),
                  0, 60)
    sex = pd.to_numeric(pd.Series(sex_code), errors="coerce").to_numpy(dtype=float)
    L = np.full(len(age), np.nan); M = L.copy(); S = L.copy()
    for s in (1, 2):
        t = lms[lms["sex"] == s].sort_values("age_month")
        k = (sex == s) & ~np.isnan(age)
        for arr, c in ((L, "L"), (M, "M"), (S, "S")):
            arr[k] = np.interp(age[k], t["age_month"], t[c])
    x = pd.to_numeric(pd.Series(height_cm), errors="coerce").to_numpy(dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (np.power(x / M, L) - 1.0) / (L * S)
    return z


# ── Main entry point ─────────────────────────────────────────────────────────────

def add_computed_stunting(df: pd.DataFrame, impute: bool = True) -> pd.DataFrame:
    """
    Return df with added per-visit columns:
        haz_calc              float   computed height-for-age z-score
        age_months_calc       float   age used (from DOB when available)
        height_clean_cm       float   height after outlier removal + imputation
        stunting_calc         str     Normal | Moderate stunting | Severe stunting
        haz_imputed           bool    True if this visit's height came from a
                                      same-day duplicate event
    """
    col = _resolve(df)
    out = df.copy()

    # Age in months (prefer DOB + visit date; fall back to recorded age)
    age = pd.Series(np.nan, index=out.index)
    if col["dob"] and col["date"]:
        d_dob = pd.to_datetime(out[col["dob"]], errors="coerce")
        d_evt = pd.to_datetime(out[col["date"]], errors="coerce")
        age = (d_evt - d_dob).dt.days / 30.4375
        age[(age < 0) | (age > 72)] = np.nan      # impossible ages
    if col["age"]:
        age = age.fillna(pd.to_numeric(out[col["age"]], errors="coerce"))
    out["_age_m"] = age

    # Sex
    out["_sex"] = _sex_code(out[col["sex"]]) if col["sex"] else np.nan

    # Height + plausibility
    h = pd.to_numeric(out[col["height"]], errors="coerce") if col["height"] else pd.Series(np.nan, index=out.index)
    h[(h < _HEIGHT_MIN) | (h > _HEIGHT_MAX)] = np.nan
    out["_height"] = h
    out["_height_raw"] = h.copy()

    # First-pass HAZ → implausible z is a data-entry error, not a measurement
    z0 = haz(out["_height"], out["_age_m"], out["_sex"])
    out.loc[np.abs(z0) > _HAZ_FLAG, "_height"] = np.nan

    # Copied or shrinking heights count as not measured — the same rule as
    # core/fast_build.suspect_heights.
    if col["tei"] and col["date"]:
        tei = col["tei"]
        dday = pd.to_datetime(out[col["date"]], errors="coerce").dt.normalize()
        m = out.assign(_day=dday)[out["_height"].notna() & dday.notna()]
        days = (m.groupby([tei, "_day"]).agg(_hd=("_height", "mean"), _ad=("_age_m", "min"))
                 .reset_index().sort_values([tei, "_day"]))
        prev_h = days.groupby(tei)["_hd"].shift(1)
        gap = (days["_day"] - days.groupby(tei)["_day"].shift(1)).dt.days
        bad = (((days["_hd"] == prev_h) & (gap >= COPY_MIN_DAYS) & (days["_ad"] < COPY_MAX_AGE))
               | (days["_hd"] < prev_h - SHRINK_CM))
        bad_keys = set(zip(days.loc[bad, tei], days.loc[bad, "_day"]))
        if bad_keys:
            hit = pd.Series(list(zip(out[tei], dday)), index=out.index).isin(bad_keys)
            out.loc[hit, "_height"] = np.nan

    # eTracker often holds two events for the same visit day — one with the
    # height, one without. The blank one is the same measurement, so it takes
    # that day's height. Nothing is borrowed from other days.
    if impute and col["tei"] and col["date"]:
        tei = col["tei"]
        day = pd.to_datetime(out[col["date"]], errors="coerce").dt.normalize()
        same_day = out.groupby([out[tei], day])["_height"].transform("mean")
        out["_height"] = out["_height"].fillna(same_day)

    out["haz_imputed"]     = out["_height"].notna() & out["_height_raw"].isna()
    out["height_clean_cm"] = out["_height"]
    out["age_months_calc"] = out["_age_m"]
    out["haz_calc"]        = haz(out["_height"], out["_age_m"], out["_sex"])

    z = out["haz_calc"]
    out["stunting_calc"] = np.select(
        [z < -3, z < -2, z.notna()],
        ["Severe stunting", "Moderate stunting", "Normal"],
        default=None,
    )

    return out.drop(columns=["_age_m", "_sex", "_height", "_height_raw"])


def classify_children(df: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse multi-visit data to ONE row per child (deduplicated by entity_id).
    Uses the most recent VALID visit for current status.

    Returns columns:
        child id, stunting_calc (current), haz_calc (current),
        ever_stunted (bool), n_visits, age_months_calc (at latest valid visit)
    """
    work = add_computed_stunting(df)
    col = _resolve(work)
    tei = col["tei"]
    if tei is None:
        return pd.DataFrame()

    date = col["date"]
    work["_order"] = (pd.to_datetime(work[date], errors="coerce")
                      if date else work["age_months_calc"])

    valid = work[work["haz_calc"].notna()].copy()
    # ever-stunted across all valid visits
    ever = (valid.assign(_s=valid["haz_calc"] < -2)
                 .groupby(tei)["_s"].max().rename("ever_stunted"))
    nvis = work.groupby(tei).size().rename("n_visits")

    # latest valid visit per child
    latest = (valid.sort_values("_order")
                   .drop_duplicates(subset=[tei], keep="last")
                   .set_index(tei))

    res = latest[["stunting_calc", "haz_calc", "age_months_calc"]].join(ever).join(nvis)
    res["ever_stunted"] = res["ever_stunted"].fillna(False)
    return res.reset_index()
