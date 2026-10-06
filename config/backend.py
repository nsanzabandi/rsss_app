"""
config/backend.py — Single switch for which database the whole app reads.

Set DATA_BACKEND in .env:
    DATA_BACKEND=local        → local immunization_db / immunization_vaccination
                                (populated by sync/etl.py from eTracker)   [default]
    DATA_BACKEND=production    → metabase DB / immunization_update over the SSH tunnel

Everything that reads immunization data (dashboard denominators, report loader,
follow-up search) goes through get_conn() + SCHEMA so flipping this one value
moves the entire app from local to production with no code changes.

SCHEMA maps logical fields → the physical column (or SQL expression) for the
active backend, so the same query code works against both table layouts.
"""
from __future__ import annotations

import os

DATA_BACKEND = os.environ.get("DATA_BACKEND", "local").strip().lower()

_SCHEMAS: dict[str, dict] = {
    "local": {
        "table":        "immunization_vaccination",
        "event_id":     "event_id",
        "tei":          "entity_id",
        "gender":       "gender",
        "age":          "age_visit_months",
        "facility":     "health_facility",
        "hospital":     "h_district_hospital",
        "district":     "COALESCE(h_district, residence_district, district_source)",
        "sector":       "h_sector",
        "stunting":     "stunting_status",
        "schedule":     "immunization_schedule",
        "weight":       "weight_visit_kg",
        "height":       "height_visit_cm",
        # Local has no severe_stunting column → derive a clean Yes/No flag.
        # NOTE: use STRPOS, not LIKE '%..%' — a literal % breaks psycopg2 when
        # the query is run with parameters ("dict is not a sequence").
        "severe":       "CASE WHEN STRPOS(LOWER(stunting_status), 'sever') > 0 THEN 'Yes' ELSE 'No' END",
        "imm_date":     "last_immunization_date",
        # Matches 'moderate stunting' / 'severe stunting' / 'stunted', excludes normal/not-stunted
        "stunted_pred": ("STRPOS(LOWER(TRIM(stunting_status)), 'stunt') > 0 "
                         "AND LEFT(LOWER(TRIM(stunting_status)), 4) <> 'not ' "
                         "AND LEFT(LOWER(TRIM(stunting_status)), 3) <> 'no '"),
    },
    "production": {
        "table":        "immunization_update",
        "tei":          "tracked_entity_instance",
        "gender":       "gender",
        "age":          "age_in_months",
        "facility":     "health_facility",
        "hospital":     "district_hospital",
        "district":     "district",
        "sector":       '"Sector"',
        "stunting":     "stunting_status",
        "schedule":     "immunization_schedule",
        "weight":       "weight_at_visit_kg",
        "height":       "height_at_visit_cm",
        "severe":       "CASE WHEN STRPOS(LOWER(severe_stunting), 'sever') > 0 THEN 'Yes' ELSE 'No' END",
        "imm_date":     "immunization_date",
        "stunted_pred": "stunting_status = 'Stunted'",
    },
}


def active() -> str:
    return DATA_BACKEND if DATA_BACKEND in _SCHEMAS else "local"


def schema() -> dict:
    return _SCHEMAS[active()]


def scope_where(user: dict | None, s: dict | None = None) -> tuple[str, list]:
    """SQL condition limiting rows to the user's own area — the SQL twin of
    data.filter_by_user(). Fails CLOSED: unknown role or missing area → no rows.
    Returns ("", []) for national roles (ministry / public aggregates)."""
    s = s or schema()
    user = user or {}
    role = user.get("role")
    if role in ("ministry", "public"):
        return "", []
    col, val = {"district":      (s["district"], user.get("district")),
                "hospital":      (s["hospital"], user.get("hospital")),
                "health_center": (s["facility"], user.get("health_center"))}.get(role, (None, None))
    if not col or not val:
        return "FALSE", []
    return f"{col} = %s", [val]


def get_conn():
    """Return a psycopg2 connection to the active backend."""
    import psycopg2
    if active() == "production":
        from config.db_config import db_config as cfg
        return psycopg2.connect(**cfg.get_connection_kwargs())
    from config.db_local import local_db
    return psycopg2.connect(**local_db.get_connection_kwargs())


def ensure_followup_table() -> None:
    """Create the stunting_followup table in the active backend if missing."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stunting_followup (
            id                       SERIAL PRIMARY KEY,
            tracked_entity_instance  TEXT,
            followup_date            DATE,
            weight_kg                NUMERIC,
            height_cm                NUMERIC,
            interventions            TEXT,
            notes                    TEXT,
            created_at               TIMESTAMP DEFAULT NOW(),
            UNIQUE (tracked_entity_instance, followup_date)
        );
        ALTER TABLE stunting_followup ADD COLUMN IF NOT EXISTS recorded_by TEXT;
    """)
    conn.commit()
    cur.close()
    conn.close()
