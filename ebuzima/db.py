"""Database helpers for the eBuzima Nutrition Analytics page.

Uses its OWN PostgreSQL instance (EBUZIMA_DB_* env vars) — a separate
connection from rsss_app's own immunization_db.

Provides:
- get_engine()         — cached SQLAlchemy engine (PostgreSQL)
- save()               — to_sql wrapper that adds a unique index for future upserts
- upsert()             — idempotent INSERT ... ON CONFLICT via staging table
- hash_pii()           — SHA-256 mask PII columns in a DataFrame
- list_tables()        — list existing tables in the public schema
- get_last_modified()  — max(modified) timestamp for incremental ingest
"""
import hashlib
import os

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

load_dotenv()   # override=False (default) — never clobber env vars set outside .env


def _env(key, default=None):
    v = os.getenv(key, default)
    return v.strip().strip('"').strip("'") if v else default


DB_DRIVER   = _env("EBUZIMA_DB_DRIVER", "postgresql+psycopg2")
DB_USER     = _env("EBUZIMA_DB_USER")
DB_PASSWORD = _env("EBUZIMA_DB_PASSWORD")
DB_HOST     = _env("EBUZIMA_DB_HOST",   "localhost")
DB_PORT     = _env("EBUZIMA_DB_PORT",   "5432")
DB_NAME     = _env("EBUZIMA_DB_NAME")

_engine = None


def get_engine():
    """Return (or create) a cached SQLAlchemy engine. Returns None if DB not configured."""
    global _engine
    if _engine is not None:
        return _engine
    if not all([DB_USER, DB_PASSWORD, DB_NAME]):
        return None
    try:
        url = URL.create(
            drivername=DB_DRIVER,
            username=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=int(DB_PORT) if DB_PORT else 5432,
            database=DB_NAME,
        )
        _engine = create_engine(url, pool_pre_ping=True)
        return _engine
    except Exception as e:
        print(f"  [ebuzima.db] engine creation failed: {e}")
        return None


def list_tables():
    """Return list of table names in the public schema."""
    engine = get_engine()
    if engine is None:
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE' "
                "ORDER BY table_name"
            )
        ).fetchall()
    return [r[0] for r in rows]


def _ensure_unique(engine, table: str, key: str) -> None:
    """Guarantee a unique index on table.key exists.

    If duplicate rows are found, keeps the row with the highest ctid (last inserted)
    before creating the index.
    """
    q = lambda c: f'"{c}"'
    idx = f"uq_{table}_{key}"
    try:
        with engine.begin() as conn:
            conn.execute(text(
                f'CREATE UNIQUE INDEX IF NOT EXISTS "{idx}" ON {q(table)} ({q(key)})'
            ))
    except Exception:
        # Duplicates exist — deduplicate, then retry
        with engine.begin() as conn:
            conn.execute(text(f"""
                DELETE FROM {q(table)} a
                USING      {q(table)} b
                WHERE a.ctid < b.ctid
                  AND a.{q(key)} = b.{q(key)}
            """))
            conn.execute(text(
                f'CREATE UNIQUE INDEX IF NOT EXISTS "{idx}" ON {q(table)} ({q(key)})'
            ))


def save(df: pd.DataFrame, table: str, if_exists: str = "replace",
         key: str = "name", chunksize: int = 500):
    """Write a DataFrame to a DB table.

    When if_exists='replace' (default), also adds a unique index on `key` so that
    subsequent incremental upserts via ON CONFLICT work correctly.
    """
    engine = get_engine()
    if engine is None:
        raise RuntimeError("eBuzima DB not configured — set EBUZIMA_DB_USER/EBUZIMA_DB_PASSWORD/EBUZIMA_DB_NAME in .env")
    df.to_sql(table, engine, if_exists=if_exists, index=False, method="multi", chunksize=chunksize)
    if key and key in df.columns and if_exists == "replace":
        try:
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table}" ADD PRIMARY KEY ("{key}")'))
        except Exception:
            try:
                with engine.begin() as conn:
                    conn.execute(text(
                        f'CREATE UNIQUE INDEX IF NOT EXISTS "uq_{table}_{key}" '
                        f'ON "{table}" ("{key}")'
                    ))
            except Exception as e:
                print(f"  [ebuzima.db] note: could not add unique index on {table}.{key}: {e}")


def _sync_columns(engine, table: str, staging: str) -> None:
    """Add any column present in the staging table but missing from the target.

    Source DocTypes (ClickHouse mirror) gain new fields over time — e.g. new
    PII-protection columns like modified_by_iv. Without this, a target table
    created earlier from a narrower schema makes every later sync that
    includes a new column fail outright with "column does not exist", even
    though the actual new data has nothing to do with that column.
    """
    q = lambda c: f'"{c}"'
    with engine.connect() as conn:
        staging_cols = conn.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = :t ORDER BY ordinal_position"
        ), {"t": staging}).fetchall()
        existing = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
        ), {"t": table}).fetchall()}

    missing = [(name, dtype) for name, dtype in staging_cols if name not in existing]
    if not missing:
        return

    with engine.begin() as conn:
        for name, dtype in missing:
            conn.execute(text(
                f'ALTER TABLE {q(table)} ADD COLUMN IF NOT EXISTS {q(name)} {dtype}'
            ))
    print(f"  [ebuzima.db] {table}: added {len(missing)} new column(s) "
          f"({', '.join(n for n, _ in missing)})")


def upsert(df: pd.DataFrame, table: str, key: str = "name",
           chunksize: int = 500) -> dict:
    """Idempotent upsert using a staging table + INSERT ... ON CONFLICT.

    Automatically creates the target table and unique index if they do not exist,
    so the first call works even on a fresh database. Also auto-migrates the
    target table's schema (adds any new columns) before inserting — see
    _sync_columns().

    Returns {"inserted": N, "updated": M} — actual counts from PostgreSQL RETURNING.
    """
    engine = get_engine()
    if engine is None:
        raise RuntimeError("eBuzima DB not configured")
    if key not in df.columns:
        raise ValueError(f"Key column '{key}' not in DataFrame")

    staging = f"{table}_stg"
    df.to_sql(staging, engine, if_exists="replace", index=False,
              method="multi", chunksize=chunksize)

    q = lambda c: f'"{c}"'
    cols = list(df.columns)
    col_list   = ", ".join(q(c) for c in cols)
    update_cols = [c for c in cols if c != key]
    set_clause  = ", ".join(f"{q(c)} = EXCLUDED.{q(c)}" for c in update_cols)

    # Create target table from staging schema if it doesn't exist yet
    with engine.begin() as conn:
        conn.execute(text(
            f'CREATE TABLE IF NOT EXISTS {q(table)} AS '
            f'SELECT * FROM {q(staging)} WHERE FALSE'
        ))

    # Add any columns the staging table has that the target is still missing
    # (existing table created earlier from a narrower/older schema).
    _sync_columns(engine, table, staging)

    # Guarantee unique constraint (handles tables created without one)
    _ensure_unique(engine, table, key)

    # INSERT with RETURNING to count true inserts vs updates.
    # xmax = 0 → row was freshly inserted; xmax ≠ 0 → row was updated.
    # DROP TABLE is executed separately so it always runs regardless of row count.
    conflict_action = (f"DO UPDATE SET {set_clause}" if set_clause else "DO NOTHING")
    upsert_sql = f"""
        WITH result AS (
            INSERT INTO {q(table)} ({col_list})
            SELECT {col_list} FROM {q(staging)}
            ON CONFLICT ({q(key)}) {conflict_action}
            RETURNING (xmax = 0) AS is_insert
        )
        SELECT
            COUNT(*) FILTER (WHERE is_insert)     AS inserted,
            COUNT(*) FILTER (WHERE NOT is_insert) AS updated
        FROM result
    """
    with engine.begin() as conn:
        row = conn.execute(text(upsert_sql)).fetchone()
        conn.execute(text(f'DROP TABLE IF EXISTS {q(staging)}'))

    inserted = int(row[0]) if row else 0
    updated  = int(row[1]) if row else 0
    return {"inserted": inserted, "updated": updated}


def get_last_modified(table: str, col: str = "modified") -> str | None:
    """Return MAX(col) from an existing table as 'YYYY-MM-DD HH:MM:SS', or None.

    Used for incremental ingestion — only fetch records modified after this timestamp.
    """
    engine = get_engine()
    if engine is None:
        return None
    if table not in list_tables():
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(f'SELECT MAX("{col}") FROM "{table}"')
            ).fetchone()
        if row and row[0]:
            return str(row[0])[:19]  # "YYYY-MM-DD HH:MM:SS"
    except Exception:
        pass
    return None


# PII columns to hash by default
_DEFAULT_PII_COLS = (
    "patient_name", "national_id", "nhis",
    "phone_number", "phone", "email", "id_number",
    "father_name", "mother_name",
)


def hash_pii(df: pd.DataFrame, cols=_DEFAULT_PII_COLS) -> pd.DataFrame:
    """SHA-256 hash PII columns in-place. Leaves NaN/empty values unchanged."""
    def _hash(v):
        if pd.isna(v) or str(v).strip() == "":
            return v
        return hashlib.sha256(str(v).encode()).hexdigest()

    for col in cols:
        if col in df.columns:
            df[col] = df[col].apply(_hash)
    return df
