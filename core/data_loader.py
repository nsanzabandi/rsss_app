"""
core/data_loader.py — Loads stunting data from PostgreSQL for report generation.

This is separate from data.py (which reads the CSV for the dashboard).
Reports always read fresh data from the database.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import psycopg2

if TYPE_CHECKING:
    from utils.logger import ReportLogger


class StuntingDataLoader:
    """
    Loads and prepares stunting data from PostgreSQL.

    Usage:
        loader = StuntingDataLoader(log)
        groups = loader.load_and_process(year, month)   # returns {hospital: df}
        counts = loader.get_vaccinated_counts_by_hospital()
    """

    def __init__(self, log: "ReportLogger"):
        self.log = log
        self.stunted_df:   pd.DataFrame | None = None
        self.all_df:       pd.DataFrame | None = None
        self._year:  int | None = None
        self._month: int | None = None

    # ── Connection ─────────────────────────────────────────────────────────────

    def _connect(self) -> psycopg2.extensions.connection:
        from config.backend import get_conn
        return get_conn()

    # ── Load ───────────────────────────────────────────────────────────────────

    def load_and_process(self, year: int, month: int) -> dict[str, pd.DataFrame]:
        """
        Load all immunization records for the given month/year from the active
        backend (local or production), filter to stunted children, group by
        district_hospital.

        Returns:
            Dict mapping hospital name → DataFrame of stunted children.
        """
        self._year  = year
        self._month = month

        from config.backend import schema, active
        s = schema()
        self.log.info(f"Loading data for {month:02d}/{year} "
                      f"from {active()} backend ({s['table']})…")

        # Core analytics columns (backend-neutral).
        event_id_col = (f", {s['event_id']} AS event_id" if s.get("event_id") else "")
        core = f"""
                {s['tei']}       AS tracked_entity_instance,
                {s['gender']}    AS gender,
                {s['age']}       AS age_in_months,
                {s['facility']}  AS health_facility,
                {s['hospital']}  AS district_hospital,
                {s['district']}  AS district,
                {s['sector']}    AS sector,
                {s['stunting']}  AS stunting_status,
                {s['severe']}    AS severe_stunting,
                {s['schedule']}  AS immunization_schedule,
                {s['weight']}    AS weight_at_visit_kg,
                {s['height']}    AS height_at_visit_cm,
                {s['imm_date']}  AS immunization_date
        """
        if active() == "local":
            # Extra columns the case-list (Excel) report needs — from the local
            # immunization_vaccination table.
            extra = """,
                child_id,
                child_name,
                date_of_birth,
                mother_names, mother_phone, mother_dob,
                father_names, father_phone,
                residence_province AS province,
                residence_province, residence_district, residence_sector,
                village
            """
        else:
            extra = ""
        # Two-step fetch: WHO HAZ computation (add_computed_stunting, below)
        # imputes a visit's missing/implausible height from that SAME
        # child's OTHER visits — but only if those other visits are present
        # in the batch it's given. Fetching only this month's rows starves
        # it of that context: a child whose only July row has a null/outlier
        # height would get an uncomputable HAZ and silently vanish from the
        # stunted count, even though the dashboard (which always loads full
        # history before scoping to a period) can impute it fine. So: first
        # find which children had any visit this month, then fetch each of
        # THEIR full visit history, compute HAZ with full context, and only
        # then narrow down to this month's (latest) visit per child.
        id_sql = f"""
            SELECT DISTINCT {s['tei']}
            FROM {s['table']}
            WHERE {s['imm_date']} >= %(start)s
              AND {s['imm_date']} <  %(end)s
        """
        # Plain range (not EXTRACT) so Postgres can use the date index.
        from datetime import date
        period_start = date(year, month, 1)
        period_end   = date(year + (month == 12), month % 12 + 1, 1)
        sql = f"""
            SELECT {core}{extra}{event_id_col}
            FROM {s['table']}
            WHERE {s['tei']} = ANY(%(ids)s)
        """
        try:
            conn = self._connect()
            cur  = conn.cursor()
            cur.execute(id_sql, {"start": period_start, "end": period_end})
            child_ids = [r[0] for r in cur.fetchall() if r[0]]
            if not child_ids:
                cur.close()
                conn.close()
                self.log.warning("No records found for this period.")
                return {}
            cur.execute(sql, {"ids": child_ids})
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            cur.close()
            conn.close()
            full_history = pd.DataFrame(rows, columns=cols)
        except Exception as exc:
            self.log.error(f"Database error: {exc}")
            return {}

        if full_history.empty:
            self.log.warning("No records found for this period.")
            return {}

        self.log.info(f"Loaded {len(full_history):,} visit records "
                      f"({len(child_ids):,} children active this period).")

        # Normalise columns
        full_history = self._normalise(full_history)

        # Compute WHO height-for-age stunting (accurate) instead of trusting the
        # eTracker column, so reports match the dashboard. Falls back to the
        # column if measurements (height/DOB/sex) are unavailable. Runs on the
        # FULL history (see above) so imputation has surrounding visits to work
        # with, then we narrow down to this month's rows right after.
        try:
            from core.stunting_calculator import add_computed_stunting
            full_history = add_computed_stunting(full_history)
        except Exception as exc:
            self.log.warning(f"HAZ compute skipped ({exc}) — using column.")

        self.all_df = full_history[
            (pd.to_datetime(full_history["immunization_date"], errors="coerce").dt.year == year) &
            (pd.to_datetime(full_history["immunization_date"], errors="coerce").dt.month == month)
        ].copy()
        if "haz_calc" in self.all_df.columns:
            haz = self.all_df["haz_calc"]
            self.all_df["severe_stunting"] = np.where(haz < -3, "Yes", "No")
            n_haz = int(haz.notna().sum())
            self.log.info(f"WHO HAZ computed for {n_haz:,}/{len(self.all_df):,} records.")

        # DEDUPLICATE to one row per child (latest visit within the period)
        # FIRST, then filter to stunted status on that latest visit. Filtering
        # before deduping would instead count anyone stunted at ANY visit
        # this period — including a child who recovered by their last visit —
        # which doesn't match the dashboard's "current (latest visit)"
        # methodology (data.build_period_child_df) and made the report and
        # dashboard totals diverge for the same period.
        tei = "tracked_entity_instance"
        latest = self.all_df
        # Latest ASSESSABLE visit, as the dashboard and trend chart do — a
        # child's unmeasurable last visit shouldn't hide an earlier valid one.
        if "haz_calc" in latest.columns and latest["haz_calc"].notna().any():
            latest = latest[latest["haz_calc"].notna()]
        if tei in latest.columns and not latest.empty:
            if "immunization_date" in latest.columns:
                # event_id as a secondary sort key so ties on the same visit
                # date resolve the same way every time — SQL doesn't
                # guarantee row order, so without this, repeated runs of the
                # exact same query could report different stunted counts.
                sort_cols = (["immunization_date", "event_id"]
                            if "event_id" in latest.columns else ["immunization_date"])
                latest = (latest.sort_values(sort_cols)
                                .drop_duplicates(subset=[tei], keep="last"))
            else:
                latest = latest.drop_duplicates(subset=[tei])
        self.stunted_df = self._filter_stunted(latest)
        self.log.info(f"Stunted children (unique): {len(self.stunted_df):,}.")

        if self.stunted_df.empty:
            return {}

        # Group by hospital
        return self._group_by_hospital()

    def _normalise(self, df: pd.DataFrame) -> pd.DataFrame:
        str_cols = [
            "district", "district_hospital", "health_facility",
            "gender", "stunting_status", "immunization_schedule",
            "province", "sector",
        ]
        for col in str_cols:
            if col in df.columns:
                df[col] = (
                    df[col].astype(str)
                    .replace(["nan", "NaN", "None", "null", ""], np.nan)
                    .str.strip()
                )

        # Canonical gender — merge 'M'/'Male' and 'F'/'Female'.
        if "gender" in df.columns:
            g = df["gender"].astype(str).str.strip().str.lower()
            df["gender"] = np.where(g.str.startswith("m"), "Male",
                            np.where(g.str.startswith("f"), "Female", None))

        if "immunization_date" in df.columns:
            df["immunization_date"] = pd.to_datetime(df["immunization_date"], errors="coerce")

        num_cols = ["age_in_months", "height_for_age_zscore",
                    "weight_at_visit_kg", "height_at_visit_cm"]
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        return df

    def _filter_stunted(self, df: pd.DataFrame) -> pd.DataFrame:
        """Filter to stunted children. Prefers the WHO computed HAZ (< -2); falls
        back to the eTracker column / stored z-score only if HAZ isn't available."""
        # 1) WHO computed height-for-age (accurate)
        if "haz_calc" in df.columns and df["haz_calc"].notna().any():
            return df[df["haz_calc"] < -2].copy()
        # 2) stored z-score
        if "height_for_age_zscore" in df.columns and \
                pd.to_numeric(df["height_for_age_zscore"], errors="coerce").notna().any():
            z = pd.to_numeric(df["height_for_age_zscore"], errors="coerce")
            return df[z <= -2].copy()
        # 3) eTracker column (last resort)
        if "stunting_status" in df.columns:
            ss = df["stunting_status"].astype(str).str.lower().str.strip()
            mask = (ss.str.contains("stunt", na=False)
                    & ~ss.str.startswith("not ")
                    & ~ss.str.startswith("no "))
            return df[mask].copy()
        return df.copy()

    def _group_by_hospital(self) -> dict[str, pd.DataFrame]:
        if self.stunted_df is None or "district_hospital" not in self.stunted_df.columns:
            return {}
        groups = {}
        for name, sub in self.stunted_df.groupby("district_hospital"):
            if pd.notna(name) and name.strip():
                groups[str(name).strip()] = sub.copy()
        self.log.info(f"Grouped into {len(groups)} hospitals.")
        return groups

    def group_by_facility(self) -> dict[str, pd.DataFrame]:
        """Group stunted_df by health_facility (call after load_and_process)."""
        if self.stunted_df is None or "health_facility" not in self.stunted_df.columns:
            return {}
        groups = {}
        for name, sub in self.stunted_df.groupby("health_facility"):
            if pd.notna(name) and name.strip():
                groups[str(name).strip()] = sub.copy()
        return groups

    # ── Vaccination counts ─────────────────────────────────────────────────────

    def get_vaccinated_counts_by_hospital(self) -> dict[str, int]:
        """Count all children vaccinated per hospital (from all_df, not just stunted)."""
        if self.all_df is None or "district_hospital" not in self.all_df.columns:
            return {}
        tei = "tracked_entity_instance"
        if tei not in self.all_df.columns:
            return self.all_df["district_hospital"].value_counts().to_dict()
        return (
            self.all_df.groupby("district_hospital")[tei].nunique().to_dict()
        )

    def get_total_vaccinated_unique(self) -> int:
        """True national unique-child count (from all_df) — NOT the sum of
        get_vaccinated_counts_by_hospital(), which double-counts any child
        who visited more than one hospital within the period."""
        if self.all_df is None or self.all_df.empty:
            return 0
        tei = "tracked_entity_instance"
        if tei in self.all_df.columns:
            return int(self.all_df[tei].nunique())
        return len(self.all_df)

    # ── Assessed counts (stunting-rate denominator) ────────────────────────────
    # A child with no usable height/DOB can't be classified, so they don't
    # belong in the prevalence denominator — same rule as the dashboard
    # (data.summarize_children) and the trend chart (data.monthly_rate).

    def _assessed_latest(self) -> pd.DataFrame | None:
        """One row per child (latest visit this period) with a computable HAZ."""
        tei = "tracked_entity_instance"
        if self.all_df is None or self.all_df.empty or "haz_calc" not in self.all_df.columns:
            return None
        df = self.all_df[self.all_df["haz_calc"].notna()]
        if tei in df.columns:
            sort_cols = (["immunization_date", "event_id"]
                         if "event_id" in df.columns else ["immunization_date"])
            df = df.sort_values(sort_cols).drop_duplicates(subset=[tei], keep="last")
        return df

    def get_total_assessed_unique(self) -> int:
        df = self._assessed_latest()
        return len(df) if df is not None else 0

    def get_assessed_counts_by(self, col: str) -> dict[str, int]:
        """Assessed children per hospital/facility, attributed to the same
        (latest) visit the stunted count uses — so numerator ⊆ denominator."""
        df = self._assessed_latest()
        if df is None or col not in df.columns:
            return {}
        return df.groupby(col).size().to_dict()

    def get_vaccinated_counts_by_facility(self) -> dict[str, int]:
        """Count all children vaccinated per health facility."""
        if self.all_df is None or "health_facility" not in self.all_df.columns:
            return {}
        tei = "tracked_entity_instance"
        if tei not in self.all_df.columns:
            return self.all_df["health_facility"].value_counts().to_dict()
        return (
            self.all_df.groupby("health_facility")[tei].nunique().to_dict()
        )
