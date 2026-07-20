"""
core/stunting_analytics.py — Statistical analysis of stunting DataFrames.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from utils.logger import ReportLogger

TEI = "tracked_entity_instance"


class StuntingAnalytics:
    def __init__(self, log: "ReportLogger"):
        self.log = log

    def calculate_statistics(self, df: pd.DataFrame) -> dict:
        """
        Compute a comprehensive stats dict from a stunted children DataFrame.

        Returns:
            {
              total_stunted_children,
              gender_distribution,
              schedule_breakdown,
              facility_distribution,
              age_distribution,
              severe_stunting_count, severe_stunting_pct,
              avg_age_months,
              avg_weight_kg, avg_height_cm,
            }
        """
        if df is None or df.empty:
            return {"total_stunted_children": 0}

        # Deduplicate on TEI for unique-child counts
        unique = df.drop_duplicates(subset=[TEI]) if TEI in df.columns else df
        total  = len(unique)

        stats: dict = {"total_stunted_children": total}

        # ── Gender ────────────────────────────────────────────────────────────
        if "gender" in df.columns:
            gen = unique[unique["gender"].notna()].groupby("gender").size()
            stats["gender_distribution"] = {
                str(k): int(v) for k, v in gen.items()
            }
        else:
            stats["gender_distribution"] = {}

        # ── Immunization schedule ─────────────────────────────────────────────
        if "immunization_schedule" in df.columns:
            sched = unique[unique["immunization_schedule"].notna()].groupby(
                "immunization_schedule"
            ).size().sort_values(ascending=False)
            stats["schedule_breakdown"] = {str(k): int(v) for k, v in sched.items()}
        else:
            stats["schedule_breakdown"] = {}

        # ── Facility distribution ─────────────────────────────────────────────
        if "health_facility" in df.columns:
            fac = unique[unique["health_facility"].notna()].groupby(
                "health_facility"
            ).size().sort_values(ascending=False)
            stats["facility_distribution"] = {str(k): int(v) for k, v in fac.items()}
        else:
            stats["facility_distribution"] = {}

        # ── Age distribution ──────────────────────────────────────────────────
        age_col = "age_in_months" if "age_in_months" in df.columns else None
        if age_col:
            ages = pd.to_numeric(unique[age_col], errors="coerce").dropna()
            if len(ages):
                bins   = [0, 6, 12, 18, 24, 36, 48, 60, 999]
                labels = ["0–6 mo", "6–12 mo", "12–18 mo", "18–24 mo",
                          "24–36 mo", "36–48 mo", "48–60 mo", "60+ mo"]
                cuts = pd.cut(ages, bins=bins, labels=labels, right=False)
                dist = cuts.value_counts().reindex(labels, fill_value=0)
                stats["age_distribution"] = {str(k): int(v) for k, v in dist.items()}
                stats["avg_age_months"] = round(float(ages.mean()), 1)
            else:
                stats["age_distribution"] = {}
                stats["avg_age_months"] = None
        else:
            stats["age_distribution"] = {}
            stats["avg_age_months"] = None

        # ── Severe stunting ───────────────────────────────────────────────────
        if "severe_stunting" in df.columns:
            sev = unique["severe_stunting"].astype(str).str.lower()
            n_sev = int((sev == "yes").sum())
        elif "height_for_age_zscore" in df.columns:
            haz = pd.to_numeric(unique["height_for_age_zscore"], errors="coerce")
            n_sev = int((haz <= -3).sum())
        else:
            n_sev = 0

        stats["severe_stunting_count"] = n_sev
        stats["severe_stunting_pct"]   = (
            round(n_sev / total * 100, 1) if total else 0
        )

        # ── Anthropometrics ───────────────────────────────────────────────────
        for col, key in [("weight_at_visit_kg", "avg_weight_kg"),
                          ("height_at_visit_cm", "avg_height_cm")]:
            if col in df.columns:
                vals = pd.to_numeric(df[col], errors="coerce").dropna()
                stats[key] = round(float(vals.mean()), 2) if len(vals) else None
            else:
                stats[key] = None

        return stats

    def get_schedule_specific_counts(self, df: pd.DataFrame) -> dict[str, dict]:
        """
        Returns per-schedule counts broken down by gender.

        Structure: { schedule_name: { "total": n, "male": m, "female": f } }
        """
        if df is None or df.empty or "immunization_schedule" not in df.columns:
            return {}

        unique = df.drop_duplicates(subset=[TEI]) if TEI in df.columns else df
        result: dict = {}

        for sched, grp in unique[unique["immunization_schedule"].notna()].groupby(
            "immunization_schedule"
        ):
            sched_str = str(sched)
            total = len(grp)
            gen_counts: dict = {}
            if "gender" in grp.columns:
                for g, cnt in grp[grp["gender"].notna()].groupby("gender").size().items():
                    gen_counts[str(g).lower()] = int(cnt)
            result[sched_str] = {"total": total, **gen_counts}

        return result
