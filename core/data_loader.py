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
        sql = f"""
            SELECT {core}{extra}
            FROM {s['table']}
            WHERE EXTRACT(YEAR  FROM {s['imm_date']}) = %(year)s
              AND EXTRACT(MONTH FROM {s['imm_date']}) = %(month)s
        """
        try:
            conn = self._connect()
            cur  = conn.cursor()
            cur.execute(sql, {"year": year, "month": month})
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            cur.close()
            conn.close()
            self.all_df = pd.DataFrame(rows, columns=cols)
        except Exception as exc:
            self.log.error(f"Database error: {exc}")
            return {}

        if self.all_df.empty:
            self.log.warning("No records found for this period.")
            return {}

        self.log.info(f"Loaded {len(self.all_df):,} records total.")

        # Normalise columns
        self.all_df = self._normalise(self.all_df)

        # Compute WHO height-for-age stunting (accurate) instead of trusting the
        # eTracker column, so reports match the dashboard. Falls back to the
        # column if measurements (height/DOB/sex) are unavailable.
        try:
            from core.stunting_calculator import add_computed_stunting
            self.all_df = add_computed_stunting(self.all_df)
            haz = self.all_df["haz_calc"]
            self.all_df["severe_stunting"] = np.where(haz < -3, "Yes", "No")
            n_haz = int(haz.notna().sum())
            self.log.info(f"WHO HAZ computed for {n_haz:,}/{len(self.all_df):,} records.")
        except Exception as exc:
            self.log.warning(f"HAZ compute skipped ({exc}) — using column.")

        # Filter stunted children, then DEDUPLICATE to one row per child (latest
        # visit) so the case list, email summary and PDF all report the same
        # number of unique children — not visit-events.
        self.stunted_df = self._filter_stunted(self.all_df)
        tei = "tracked_entity_instance"
        if tei in self.stunted_df.columns and not self.stunted_df.empty:
            if "immunization_date" in self.stunted_df.columns:
                self.stunted_df = (self.stunted_df
                                   .sort_values("immunization_date")
                                   .drop_duplicates(subset=[tei], keep="last"))
            else:
                self.stunted_df = self.stunted_df.drop_duplicates(subset=[tei])
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
