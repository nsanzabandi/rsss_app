"""
core/national_summary_analytics.py — National-level summary analytics.

Note: uses 'age_in_months' (the actual CSV column name).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from utils.logger import ReportLogger

TEI = "tracked_entity_instance"


class NationalSummaryAnalytics:
    def __init__(self, log: "ReportLogger"):
        self.log = log

    def _unique(self, df: pd.DataFrame) -> pd.DataFrame:
        if TEI in df.columns:
            return df.drop_duplicates(subset=[TEI])
        return df

    # ── Core calculations ──────────────────────────────────────────────────────

    def calculate_age_distribution(self, combined_df: pd.DataFrame) -> dict:
        """Age distribution in 6-month bands."""
        age_col = "age_in_months" if "age_in_months" in combined_df.columns else "child_age_months"
        if age_col not in combined_df.columns:
            return {}

        unique = self._unique(combined_df)
        ages   = pd.to_numeric(unique[age_col], errors="coerce").dropna()
        total  = len(ages)
        if total == 0:
            return {}

        bands = {
            "0–6 months":   int((ages < 6).sum()),
            "6–12 months":  int(((ages >= 6)  & (ages < 12)).sum()),
            "12–15 months": int(((ages >= 12) & (ages < 15)).sum()),
            "15+ months":   int((ages >= 15).sum()),
        }

        return {
            band: {
                "count":      count,
                "percentage": round(count / total * 100, 1) if total else 0,
            }
            for band, count in bands.items()
        }

    def calculate_average_age(self, combined_df: pd.DataFrame) -> float:
        """Average age in months across unique children."""
        age_col = "age_in_months" if "age_in_months" in combined_df.columns else "child_age_months"
        if age_col not in combined_df.columns:
            return 0
        unique  = self._unique(combined_df)
        avg     = pd.to_numeric(unique[age_col], errors="coerce").mean()
        return round(float(avg), 1) if pd.notna(avg) else 0

    def calculate_gender_distribution(self, combined_df: pd.DataFrame) -> dict:
        if "gender" not in combined_df.columns:
            return {}
        unique = self._unique(combined_df)
        gen    = unique[unique["gender"].notna()].groupby("gender").size()
        total  = gen.sum()
        return {
            str(g): {
                "count":      int(n),
                "percentage": round(n / total * 100, 1) if total else 0,
            }
            for g, n in gen.items()
        }

    def calculate_province_distribution(self, combined_df: pd.DataFrame) -> dict:
        if "province" not in combined_df.columns:
            return {}
        unique = self._unique(combined_df)
        prov   = unique[unique["province"].notna()].groupby("province").size().sort_values(ascending=False)
        total  = prov.sum()
        return {
            str(p): {
                "count":      int(n),
                "percentage": round(n / total * 100, 1) if total else 0,
            }
            for p, n in prov.items()
        }

    def get_district_summary(self, all_df: pd.DataFrame, stunted_df: pd.DataFrame) -> list[dict]:
        """
        Per-district breakdown for ALL districts (not just a top-N chart) —
        district, vaccinated (n), stunted, prevalence % — same methodology
        as the dashboard's Geographic Hotspots chart, sorted worst-first.
        """
        if "district" not in all_df.columns:
            return []
        vacc = self._unique(all_df[all_df["district"].notna()]).groupby("district").size()
        stu  = (self._unique(stunted_df[stunted_df["district"].notna()]).groupby("district").size()
               if stunted_df is not None and not stunted_df.empty and "district" in stunted_df.columns
               else pd.Series(dtype=int))
        rows = []
        for district, n in vacc.items():
            s = int(stu.get(district, 0))
            rows.append({
                "district": str(district),
                "n":        int(n),
                "stunted":  s,
                "rate":     round(s / n * 100, 1) if n else 0,
            })
        return sorted(rows, key=lambda r: r["rate"], reverse=True)

    def get_hospital_summary(self, all_hospitals_data: dict) -> list[dict]:
        """
        Returns a list of dicts [{hospital, total, severe, avg_age, ...}],
        sorted by total descending.
        """
        rows = []
        for hospital, df in all_hospitals_data.items():
            if df is None or df.empty:
                continue
            unique = self._unique(df)
            total  = len(unique)

            # Severe
            if "severe_stunting" in df.columns:
                sev = int((unique["severe_stunting"].astype(str).str.lower() == "yes").sum())
            elif "height_for_age_zscore" in df.columns:
                haz = pd.to_numeric(unique["height_for_age_zscore"], errors="coerce")
                sev = int((haz <= -3).sum())
            else:
                sev = 0

            # Avg age
            age_col = "age_in_months" if "age_in_months" in df.columns else None
            avg_age = None
            if age_col:
                avg_age = pd.to_numeric(unique[age_col], errors="coerce").mean()
                avg_age = round(float(avg_age), 1) if pd.notna(avg_age) else None

            # District
            district = df["district"].iloc[0] if "district" in df.columns else ""

            rows.append({
                "hospital":  hospital,
                "district":  district,
                "total":     total,
                "severe":    sev,
                "sev_pct":   round(sev / total * 100, 1) if total else 0,
                "avg_age":   avg_age,
            })

        return sorted(rows, key=lambda x: x["total"], reverse=True)

    def get_most_common_schedule(self, combined_df: pd.DataFrame) -> str:
        if "immunization_schedule" not in combined_df.columns:
            return "N/A"
        sched = combined_df["immunization_schedule"].dropna().value_counts()
        return str(sched.index[0]) if not sched.empty else "N/A"

    def get_top_hospitals(
        self, all_hospitals_data: dict, top_n: int = 10
    ) -> list[dict]:
        return self.get_hospital_summary(all_hospitals_data)[:top_n]
