"""
utils/date_helpers.py — Date utility functions.
"""
from __future__ import annotations

from datetime import date, datetime

MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}


def month_label(month: int, year: int) -> str:
    """E.g. 'March 2025'"""
    return f"{MONTH_NAMES.get(month, str(month))} {year}"


def previous_month(year: int, month: int) -> tuple[int, int]:
    """Returns (year, month) for the month before the given one."""
    if month == 1:
        return year - 1, 12
    return year, month - 1


def month_date_range(year: int, month: int) -> tuple[date, date]:
    """Returns (first_day, last_day) for the given month."""
    import calendar
    first = date(year, month, 1)
    last  = date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def safe_date(value) -> date | None:
    """Parse a date from various types; return None on failure."""
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.date() if isinstance(value, datetime) else value
    try:
        return datetime.fromisoformat(str(value)).date()
    except (ValueError, TypeError):
        return None


def age_in_months(dob: date | None, reference: date | None = None) -> float | None:
    """Calculate age in months from date of birth."""
    if dob is None:
        return None
    ref = reference or date.today()
    delta = ref - dob
    return round(delta.days / 30.4375, 1)
