"""
core/stunting_calculator.py — Compute stunting ourselves from raw measurements,
instead of trusting the eTracker `stunting_status` column.

Method (WHO Child Growth Standards, height/length-for-age):
  • age (months) = (visit date − date of birth) / 30.4375   [falls back to the
    recorded age column when DOB or visit date is missing]
  • HAZ (height-for-age z-score) via the WHO LMS method:
        Z = ((height / M) ** L − 1) / (L · S)
    with sex- and age-specific L, M, S from config/who_hfa_lms.csv (0–60 months).
  • Classification (per visit):
        HAZ < −3  → Severe stunting
        −3 ≤ HAZ < −2 → Moderate stunting
        HAZ ≥ −2  → Normal
  • Outliers/missing heights are imputed from the SAME child's other visits,
    interpolated by age (entity_id keyed). Biologically implausible heights and
    |HAZ| > 6 are treated as errors and re-imputed; if a child has no usable
    measurement at all, that visit stays unclassified.

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
    aligned. Returns np.ndarray of HAZ (NaN where inputs are unusable)."""
    lms = _load_lms()
    age_round = np.clip(np.round(pd.to_numeric(pd.Series(age_months), errors="coerce")),
                        0, 60)
    key = pd.DataFrame({
        "sex":       pd.to_numeric(pd.Series(sex_code), errors="coerce").values,
        "age_month": age_round.values,
    })
    merged = key.merge(lms, on=["sex", "age_month"], how="left")
    M = merged["M"].to_numpy(dtype=float)
    S = merged["S"].to_numpy(dtype=float)
    L = merged["L"].to_numpy(dtype=float)
    x = pd.to_numeric(pd.Series(height_cm), errors="coerce").to_numpy(dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (np.power(x / M, L) - 1.0) / (L * S)
    return z


# ── Per-child imputation ────────────────────────────────────────────────────────

def _impute_group(g: pd.DataFrame) -> pd.Series:
    """Interpolate a single child's missing/outlier heights.

    Rows arrive already sorted by age within the child, so a positional linear
    interpolation fills gaps in growth order. (Positional avoids the 'NaN in
    index' problem when a visit has no recorded age.)
    """
    s = g["_height"].reset_index(drop=True)
    if s.notna().sum() == 0:
        return pd.Series(g["_height"].values, index=g.index)
    filled = s.interpolate(method="linear", limit_direction="both")
    return pd.Series(filled.values, index=g.index)


# ── Main entry point ─────────────────────────────────────────────────────────────

def add_computed_stunting(df: pd.DataFrame, impute: bool = True) -> pd.DataFrame:
    """
    Return df with added per-visit columns:
        haz_calc              float   computed height-for-age z-score
        age_months_calc       float   age used (from DOB when available)
        height_clean_cm       float   height after outlier removal + imputation
        stunting_calc         str     Normal | Moderate stunting | Severe stunting
        haz_imputed           bool    True if this visit's height was imputed
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

    # First-pass HAZ → flag implausible z as outliers to be re-imputed
    z0 = haz(out["_height"], out["_age_m"], out["_sex"])
    out.loc[np.abs(z0) > _HAZ_FLAG, "_height"] = np.nan

    # Impute within each child by age — but only touch children that actually
    # need it (more than one visit AND at least one missing/outlier height AND
    # at least one usable height to interpolate from). This keeps it fast on
    # millions of rows where most children have a single visit.
    if impute and col["tei"]:
        tei = col["tei"]
        out = out.sort_values([tei, "_age_m"])
        size  = out.groupby(tei)["_height"].transform("size")
        valid = out.groupby(tei)["_height"].transform("count")   # non-null count
        needs = (size > 1) & (valid > 0) & (valid < size)
        if needs.any():
            sub = out[out[tei].isin(out.loc[needs, tei].unique())]
            try:
                filled = sub.groupby(tei, group_keys=False).apply(
                    _impute_group, include_groups=False)
            except TypeError:   # older pandas without include_groups
                filled = sub.groupby(tei, group_keys=False).apply(_impute_group)
            out.loc[filled.index, "_height"] = filled

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
