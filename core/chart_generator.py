"""
core/chart_generator.py — Matplotlib chart generation for PDF reports.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import matplotlib
matplotlib.use("Agg")   # non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

if TYPE_CHECKING:
    from utils.logger import ReportLogger

# Brand palette
_RED     = "#C0392B"
_BLUE    = "#2980B9"
_GREEN   = "#27AE60"
_ORANGE  = "#E67E22"
_PURPLE  = "#8E44AD"
_TEAL    = "#1ABC9C"

_PALETTE = [_RED, _BLUE, _GREEN, _ORANGE, _PURPLE, _TEAL,
            "#F39C12", "#2C3E50", "#D35400", "#16A085"]


class ChartGenerator:
    def __init__(self, log: "ReportLogger"):
        self.log = log

    # ── Gender pie ─────────────────────────────────────────────────────────────

    def create_gender_pie_chart(self, gender_data: dict, output_path: str) -> bool:
        """
        Creates a gender pie chart.
        gender_data: {"Male": 120, "Female": 98, ...}
        """
        if not gender_data:
            return False
        try:
            labels = list(gender_data.keys())
            sizes  = list(gender_data.values())
            colors = [_BLUE, _RED, _GREEN, _ORANGE][:len(labels)]

            fig, ax = plt.subplots(figsize=(5, 4))
            wedges, texts, autotexts = ax.pie(
                sizes, labels=None, colors=colors,
                autopct="%1.1f%%", startangle=90,
                wedgeprops=dict(edgecolor="white", linewidth=1.5),
            )
            for at in autotexts:
                at.set_fontsize(10)
                at.set_color("white")
                at.set_fontweight("bold")

            ax.legend(
                wedges, [f"{l} ({s:,})" for l, s in zip(labels, sizes)],
                loc="lower center", ncol=len(labels),
                fontsize=9, frameon=False,
                bbox_to_anchor=(0.5, -0.05),
            )
            ax.set_title("Gender Distribution", fontsize=11, fontweight="bold", pad=10)
            fig.tight_layout()
            fig.savefig(output_path, dpi=150, bbox_inches="tight",
                        facecolor="white", edgecolor="none")
            plt.close(fig)
            return True
        except Exception as exc:
            self.log.error(f"Gender chart error: {exc}")
            return False

    # ── Schedule bar ───────────────────────────────────────────────────────────

    def create_schedule_bar_chart(self, schedule_data: dict, output_path: str) -> bool:
        """
        Creates a horizontal bar chart of stunted children per immunization schedule.
        schedule_data: {"6 weeks": 45, "10 weeks": 38, ...}
        """
        if not schedule_data:
            return False
        try:
            items  = sorted(schedule_data.items(), key=lambda x: x[1])
            labels = [i[0] for i in items]
            values = [i[1] for i in items]
            colors = [_PALETTE[i % len(_PALETTE)] for i in range(len(labels))]

            fig_h  = max(3, len(labels) * 0.45 + 1)
            fig, ax = plt.subplots(figsize=(7, fig_h))
            bars = ax.barh(labels, values, color=colors, edgecolor="white",
                           linewidth=0.5, height=0.65)
            for bar, val in zip(bars, values):
                ax.text(bar.get_width() + max(values) * 0.01, bar.get_y() + bar.get_height() / 2,
                        f"{val:,}", va="center", fontsize=9)

            ax.set_title("Stunted Children by Immunization Schedule",
                         fontsize=11, fontweight="bold", pad=10)
            ax.set_xlabel("Number of children", fontsize=9)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.grid(axis="x", linestyle="--", alpha=0.4)
            fig.tight_layout()
            fig.savefig(output_path, dpi=150, bbox_inches="tight",
                        facecolor="white", edgecolor="none")
            plt.close(fig)
            return True
        except Exception as exc:
            self.log.error(f"Schedule chart error: {exc}")
            return False

    # ── Facility bar ───────────────────────────────────────────────────────────

    def create_facility_distribution_chart(
        self, facility_data: dict, output_path: str, top_n: int = 15
    ) -> bool:
        """
        Creates a bar chart of stunted children per health facility.
        """
        if not facility_data:
            return False
        try:
            items  = sorted(facility_data.items(), key=lambda x: x[1], reverse=True)[:top_n]
            labels = [i[0] for i in items]
            values = [i[1] for i in items]
            colors = [_RED if v == max(values) else _BLUE for v in values]

            fig_h  = max(4, len(labels) * 0.5 + 1.5)
            fig, ax = plt.subplots(figsize=(8, fig_h))
            bars = ax.barh(labels[::-1], values[::-1], color=colors[::-1],
                           edgecolor="white", linewidth=0.5, height=0.7)
            for bar, val in zip(bars, values[::-1]):
                ax.text(bar.get_width() + max(values) * 0.01, bar.get_y() + bar.get_height() / 2,
                        f"{val:,}", va="center", fontsize=8)

            ax.set_title(f"Top {len(items)} Facilities by Stunted Children",
                         fontsize=11, fontweight="bold", pad=10)
            ax.set_xlabel("Number of children", fontsize=9)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.grid(axis="x", linestyle="--", alpha=0.4)
            ax.tick_params(axis="y", labelsize=8)
            fig.tight_layout()
            fig.savefig(output_path, dpi=150, bbox_inches="tight",
                        facecolor="white", edgecolor="none")
            plt.close(fig)
            return True
        except Exception as exc:
            self.log.error(f"Facility chart error: {exc}")
            return False

    # ── Age distribution bar ────────────────────────────────────────────────────

    def create_age_distribution_chart(self, age_data: dict, output_path: str) -> bool:
        if not age_data:
            return False
        try:
            labels = list(age_data.keys())
            values = list(age_data.values())
            colors = [_PALETTE[i % len(_PALETTE)] for i in range(len(labels))]

            fig, ax = plt.subplots(figsize=(7, 3.5))
            ax.bar(labels, values, color=colors, edgecolor="white", linewidth=0.5)
            for i, v in enumerate(values):
                ax.text(i, v + max(values) * 0.01, f"{v:,}",
                        ha="center", va="bottom", fontsize=8)

            ax.set_title("Age Distribution", fontsize=11, fontweight="bold", pad=10)
            ax.set_ylabel("Number of children", fontsize=9)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.grid(axis="y", linestyle="--", alpha=0.4)
            ax.tick_params(axis="x", labelsize=8)
            fig.tight_layout()
            fig.savefig(output_path, dpi=150, bbox_inches="tight",
                        facecolor="white", edgecolor="none")
            plt.close(fig)
            return True
        except Exception as exc:
            self.log.error(f"Age chart error: {exc}")
            return False
