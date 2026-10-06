"""
core/fast_build.py — The per-child cache build in Polars (multi-core, columnar).

Produces exactly the tables data._build_child_and_monthly() produces — child,
monthly, schedule, visits, risk — with the same rules as:
  • core/stunting_calculator.add_computed_stunting  (WHO height-for-age + imputation)
  • core/risk_classifier.classify_visits            (growth-velocity risk + reasons)
  • core/risk_classifier.flag_missed_vaccinations   (Rwanda EPI schedule)
but ~10× faster: pandas ran those row-by-row on one core (per-child Python
callbacks, cell-by-cell checks, repeated object sorts); Polars does them as
whole-column operations on every core.

Outputs are pandas DataFrames, so the dashboards are unchanged. data.py falls
back to the original pandas build if Polars is unavailable.

Intentional difference — determinism: the pandas code depended on the order
rows came back from the database in two places (visits at the same age during
height imputation; same-date visits in the growth-velocity step), so the same
data could give slightly different results run to run (verified: shuffling
the rows flipped ~1,400 stunting classifications). Here every tie is broken
by date, then event_id — the same data always gives the same answer.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

TEI = "tracked_entity_instance"
_LMS_PATH = Path(__file__).resolve().parent.parent / "config" / "who_hfa_lms.csv"
_MALE   = ["1", "m", "male", "boy", "garcon", "garçon", "umuhungu", "gabo"]
_FEMALE = ["2", "f", "female", "girl", "fille", "umukobwa", "gore"]
_GEO    = ["province", "district", "district_hospital", "health_facility"]
_GIVEN  = ["yes", "1", "true", "given", "done", "administered"]


def _lms() -> pl.DataFrame:
    return pl.read_csv(_LMS_PATH).with_columns(pl.col("sex").cast(pl.Float64),
                                               pl.col("age_month").cast(pl.Float64))


def _with_haz(df: pl.DataFrame, hcol: str, out: str, lms: pl.DataFrame) -> pl.DataFrame:
    """WHO LMS z-score: ((h / M) ** L - 1) / (L · S), age rounded and clipped to 0–60."""
    j = (df.with_columns(_am=pl.col("_age_m").round(0).clip(0, 60))
           .join(lms, left_on=["_sex", "_am"], right_on=["sex", "age_month"], how="left"))
    return j.with_columns(
        (((pl.col(hcol) / pl.col("M")) ** pl.col("L") - 1) / (pl.col("L") * pl.col("S"))).alias(out)
    ).drop(["L", "M", "S", "_am"])


def computed_stunting(df: pl.DataFrame) -> pl.DataFrame:
    """add_computed_stunting(): age (DOB + visit, else recorded), sex code,
    plausible height, first-pass HAZ (|z|>6 → outlier), per-child imputation
    ordered by age (interior linear, edges nearest), final haz_calc."""
    lms = _lms()
    sexs = pl.col("gender").cast(pl.Utf8).str.strip_chars().str.to_lowercase()
    age = (pl.col("immunization_date").cast(pl.Date) - pl.col("date_of_birth").cast(pl.Date)).dt.total_days() / 30.4375
    df = df.with_columns(
        _age_m=pl.when(age.is_between(0, 72)).then(age).otherwise(None)
                 .fill_null(pl.col("age_in_months").cast(pl.Float64, strict=False)),
        _sex=pl.when(sexs.is_in(_MALE)).then(1.0).when(sexs.is_in(_FEMALE)).then(2.0).otherwise(None),
        _h=pl.col("height_at_visit_cm").cast(pl.Float64, strict=False),
    ).with_columns(_h=pl.when(pl.col("_h").is_between(38.0, 140.0)).then(pl.col("_h")).otherwise(None))
    df = df.with_columns(_hraw=pl.col("_h"))
    df = _with_haz(df, "_h", "_z0", lms)
    df = df.with_columns(_h=pl.when(pl.col("_z0").abs() > 6).then(None).otherwise(pl.col("_h")))
    # Order each child's visits by age, then date, then event_id. pandas used
    # (child, age) only, so visits at the same age kept the database's row
    # order and imputed heights — and ~1,400 stunting classifications — could
    # change between runs. With the full tie-break the result is deterministic.
    df = (df.sort([TEI, "_age_m", "immunization_date", "event_id"], nulls_last=True)
            .with_columns(_h=pl.col("_h").interpolate().forward_fill().backward_fill().over(TEI)))
    df = _with_haz(df, "_h", "haz_calc", lms)
    return df.rename({"_age_m": "age_months_calc"}).drop(["_z0", "_hraw", "_h"])


def _who_min(age: pl.Expr) -> tuple[pl.Expr, pl.Expr]:
    """WHO 3rd-centile minimum monthly gain by age bracket (risk_classifier._WHO_VEL)."""
    from core.risk_classifier import _WHO_VEL
    w = h = None
    for a0, a1, wmin, hmin in _WHO_VEL:
        cond = (age >= a0) & (age < a1)
        w = pl.when(cond).then(wmin) if w is None else w.when(cond).then(wmin)
        h = pl.when(cond).then(hmin) if h is None else h.when(cond).then(hmin)
    return w.otherwise(_WHO_VEL[-1][2]), h.otherwise(_WHO_VEL[-1][3])


def risk_visits(df: pl.DataFrame) -> pl.DataFrame:
    """classify_visits(): per weighed visit, velocity since the previous visit,
    risk_level (HIGH/MEDIUM/OK) and risk_flags (REASONS bit mask)."""
    from core.risk_classifier import (_MUAC_HIGH, _MUAC_MEDIUM, _MEDIUM_WEIGHT_FRACTION,
                                      _MEDIUM_HEIGHT_FRACTION)
    d = (pl.col("immunization_date").cast(pl.Date) - pl.col("date_of_birth").cast(pl.Date)).dt.total_days()
    r = df.with_columns(
        _w=pl.col("weight_at_visit_kg").cast(pl.Float64, strict=False),
        _hh=pl.col("height_at_visit_cm").cast(pl.Float64, strict=False),
        _muac=pl.col("muac_cm").cast(pl.Utf8).str.extract(r"(\d+\.?\d*)", 1).cast(pl.Float64, strict=False)
              if "muac_cm" in df.columns else pl.lit(None, pl.Float64),
        _wast=pl.col("wasting_status").cast(pl.Utf8).fill_null("").str.to_lowercase()
              if "wasting_status" in df.columns else pl.lit(""),
        _age=pl.when(d >= 0).then(d / 30.4375).otherwise(None)
               .fill_null(pl.col("age_in_months").cast(pl.Float64, strict=False)),
    ).with_columns(
        _age=pl.when(pl.col("_age").is_between(0, 84)).then(pl.col("_age")).otherwise(None),
        _w=pl.when(pl.col("_w").is_between(1.5, 35)).then(pl.col("_w")).otherwise(None),
        _hh=pl.when(pl.col("_hh").is_between(40, 130)).then(pl.col("_hh")).otherwise(None),
    ).filter(pl.col("immunization_date").is_not_null() & pl.col("_w").is_not_null())

    r = r.sort([TEI, "immunization_date", "event_id"]).with_columns(
        _wp=pl.col("_w").shift(1).over(TEI),
        _hp=pl.col("_hh").shift(1).over(TEI),
        _dp=pl.col("immunization_date").shift(1).over(TEI),
    ).with_columns(
        _months=(pl.col("immunization_date").cast(pl.Date) - pl.col("_dp").cast(pl.Date))
                .dt.total_days().clip(lower_bound=1) / 30.4375)
    wv = (pl.col("_w") - pl.col("_wp")) / pl.col("_months")
    hv = (pl.col("_hh") - pl.col("_hp")) / pl.col("_months")
    r = r.with_columns(
        weight_velocity=pl.when(pl.col("_wp").is_null() | (wv.abs() > 3.0)).then(None).otherwise(wv),
        height_velocity=pl.when(pl.col("_wp").is_null() | (hv.abs() > 10.0)).then(None).otherwise(hv),
    )
    muac, wvel, hvel, age, wast = (pl.col("_muac"), pl.col("weight_velocity"),
                                   pl.col("height_velocity"), pl.col("_age"), pl.col("_wast"))
    wmin, hmin = _who_min(age)
    high = ((muac < _MUAC_HIGH) | wast.str.contains("severe") | (wvel < 0)).fill_null(False)
    med_v = (age.is_not_null() & ((wvel < wmin * _MEDIUM_WEIGHT_FRACTION) |
                                  (hvel < hmin * _MEDIUM_HEIGHT_FRACTION)).fill_null(False))
    med_o = ((muac < _MUAC_MEDIUM) | wast.str.contains("moderate") | wast.str.contains("wasted")).fill_null(False)
    severe_w = wast.str.contains("severe")
    flags = (pl.when(wvel < 0).then(1).otherwise(0)
             + pl.when(muac < _MUAC_HIGH).then(2).otherwise(0)
             + pl.when(severe_w).then(4).otherwise(0)
             + pl.when((muac >= _MUAC_HIGH) & (muac < _MUAC_MEDIUM)).then(8).otherwise(0)
             + pl.when((wast.str.contains("moderate") | wast.str.contains("wasted")) & ~severe_w).then(16).otherwise(0)
             + pl.when(age.is_not_null() & (wvel >= 0) & (wvel < wmin * _MEDIUM_WEIGHT_FRACTION)).then(32).otherwise(0)
             + pl.when(age.is_not_null() & (hvel < hmin * _MEDIUM_HEIGHT_FRACTION)).then(64).otherwise(0))
    return r.with_columns(
        risk_level=pl.when(high).then(pl.lit("HIGH")).when(med_v | med_o).then(pl.lit("MEDIUM"))
                     .otherwise(pl.lit("OK")),
        risk_flags=flags.fill_null(0).cast(pl.Int16),
    )


def missed_flags(latest: pl.DataFrame) -> pl.Series:
    """flag_missed_vaccinations(): True if any EPI dose is past its window and not given
    (age from the recorded age_in_months, as the pandas version does on this table)."""
    from core.risk_classifier import _EPI_SCHEDULE
    age = pl.col("age_in_months").cast(pl.Float64, strict=False)
    missed = pl.lit(False)
    for col, _name, _lo, due_max in _EPI_SCHEDULE:
        if col in latest.columns:
            given = pl.col(col).cast(pl.Utf8).str.strip_chars().str.to_lowercase().is_in(_GIVEN).fill_null(False)
            missed = missed | ((age >= due_max).fill_null(False) & ~given)
    return latest.select(missed.alias("m"))["m"]


def load_measurements(ids: list | None = None) -> pl.DataFrame | None:
    """The measurement rows (data._MEAS_SQL), straight into Polars — no pandas
    round trip (converting 3.9M object rows cost ~10s on its own).
    Whole table → connectorx (columnar, parallel); a list of children →
    psycopg2 with a bound parameter, rows built straight into Polars."""
    import data as d
    from config.db_local import local_db
    if ids is None:
        from urllib.parse import quote
        k = local_db.get_connection_kwargs()
        uri = (f"postgresql://{quote(str(k['user']))}:{quote(str(k['password']))}"
               f"@{k['host']}:{k['port']}/{k['dbname']}")
        df = pl.read_database_uri(d._MEAS_SQL, uri, engine="connectorx")
    else:
        from config.db_local import get_local_conn
        conn = get_local_conn(); cur = conn.cursor()
        cur.execute(d._MEAS_SQL + " WHERE entity_id = ANY(%s)", (ids,))
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
        cur.close(); conn.close()
        df = pl.DataFrame(rows, schema=cols, orient="row", infer_schema_length=None)
    return df if df.height else None


def build_tables(meas):
    """Same contract as data._build_child_and_monthly(meas): returns
    (child, monthly, schedule, visits, risk) as pandas DataFrames.
    meas: a Polars frame (from load_measurements) or a pandas frame."""
    import data as d

    m = meas if isinstance(meas, pl.DataFrame) else pl.from_pandas(meas)
    # numeric/date columns arrive as Decimal/str from some drivers — normalise
    for c in ("height_at_visit_cm", "weight_at_visit_kg", "age_in_months"):
        if c in m.columns:
            m = m.with_columns(pl.col(c).cast(pl.Float64, strict=False))
    for c in ("immunization_date", "date_of_birth", "next_visit_date"):
        if c in m.columns and m.schema[c] not in (pl.Date, pl.Datetime):
            m = m.with_columns(pl.col(c).cast(pl.Utf8).str.to_date(strict=False))
    v = computed_stunting(m)
    v = v.with_columns(_o=pl.col("immunization_date").cast(pl.Datetime("us"), strict=False))
    geo = [c for c in _GEO if c in v.columns]
    order = ["_o", "event_id"] if "event_id" in v.columns else ["_o"]
    v = v.sort(order, nulls_last=True)        # pandas puts undated visits last, so they count as "latest"

    latest_all = v.unique(subset=[TEI], keep="last", maintain_order=True)
    valid = v.filter(pl.col("haz_calc").is_not_null())
    latest_valid = valid.unique(subset=[TEI], keep="last", maintain_order=True)
    ever = valid.group_by(TEI).agg(ever_stunted=(pl.col("haz_calc") < -2).any())

    _sexlab = pl.when(pl.col("_sex") == 1).then(pl.lit("Male")).when(pl.col("_sex") == 2).then(pl.lit("Female"))
    child = latest_all.select([TEI] + geo + [pl.col("_o").alias("immunization_date"), "age_months_calc"])
    child = child.join(latest_valid.select(TEI, is_stunted=pl.col("haz_calc") < -2,
                                           is_severe=pl.col("haz_calc") < -3), on=TEI, how="left")
    child = child.join(ever, on=TEI, how="left").with_columns(
        ever_stunted=pl.col("ever_stunted").fill_null(False),
        classified=pl.col("is_stunted").is_not_null(),
    )
    child = child.join(latest_all.select(TEI).with_columns(missed=missed_flags(latest_all)), on=TEI, how="left")

    # growth risk; each child's current risk = their latest weighed visit
    r = risk_visits(m)
    stunted_by_event = valid.select("event_id", _st=pl.col("haz_calc") < -2).unique("event_id")
    r = r.join(stunted_by_event, on="event_id", how="left")
    lr = (r.sort(["immunization_date"], maintain_order=True)
           .unique(subset=[TEI], keep="last", maintain_order=True)
           .select(TEI, "risk_level", "risk_flags"))
    child = child.join(lr, on=TEI, how="left").with_columns(
        risk_level=pl.col("risk_level").fill_null("OK"),
        risk_flags=pl.col("risk_flags").fill_null(0).cast(pl.Int16),
    ).with_columns(under_risk=pl.col("risk_level").is_in(["HIGH", "MEDIUM"]))
    child = child.join(latest_all.select(TEI, sex=_sexlab), on=TEI, how="left")

    # ── to the pandas shapes the dashboards use ────────────────────────────────
    child_pd = child.to_pandas()
    child_pd["missed"] = child_pd["missed"].fillna(False).astype(bool)
    child_pd["is_stunted"] = child_pd["is_stunted"].astype(object).where(child_pd["is_stunted"].notna(), np.nan)
    child_pd["is_severe"] = child_pd["is_severe"].astype(object).where(child_pd["is_severe"].notna(), np.nan)

    vcols = [c for c in ["event_id"] + geo + ["immunization_schedule"] if c in valid.columns]
    visits = valid.select([TEI] + vcols + [pl.col("_o").alias("immunization_date"),
                                           (pl.col("haz_calc") < -2).alias("is_stunted"),
                                           (pl.col("haz_calc") < -3).alias("is_severe"),
                                           _sexlab.alias("sex")])
    visits_pd = d._compact(visits.to_pandas())

    risk_pd = pd.DataFrame()
    if r.height:
        rk = r.select([TEI, pl.col("immunization_date").cast(pl.Datetime("us"), strict=False)]
                      + [c for c in geo if c in r.columns]
                      + ["risk_level", "risk_flags",
                         pl.col("weight_velocity").cast(pl.Float32),
                         pl.col("_age").cast(pl.Float32).alias("age_months"),
                         pl.col("_st").alias("is_stunted")]).to_pandas()
        rk["risk_level"] = pd.Categorical(rk["risk_level"], categories=["OK", "MEDIUM", "HIGH"])
        rk["risk_flags"] = rk["risk_flags"].astype("int16")
        rk["is_stunted"] = rk["is_stunted"].astype(object).where(rk["is_stunted"].notna(), np.nan)
        risk_pd = d._compact(rk)

    monthly, schedule = d._aggregate_visits(visits_pd)
    return child_pd, monthly, schedule, visits_pd, risk_pd
