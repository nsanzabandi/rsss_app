"""ClickHouse data source for eBuzima nutrition data.

Uses its OWN ClickHouse instance (EBUZIMA_CLICKHOUSE_* env vars) — the CDC
mirror of the eBuzima Frappe system, entirely separate from rsss_app's own
eTracker/DHIS2 pipeline.

Provides:
- get_client()   — cached clickhouse_connect client
- list_tables()  — list tables in the configured database (use this to
                   confirm real table names — see TABLE_MAP note in ingest.py)
- get_count()    — row count for a table, optional WHERE clause
- fetch_all()    — full table (or filtered subset) as a pandas DataFrame
"""
import os
import time

from dotenv import load_dotenv

load_dotenv()   # override=False (default) — never clobber env vars set outside .env

import clickhouse_connect


def _env(key, default=None):
    v = os.getenv(key, default)
    return v.strip().strip('"').strip("'") if v else default


CH_HOST     = _env("EBUZIMA_CLICKHOUSE_HOST", "197.243.95.44")
CH_PORT     = int(_env("EBUZIMA_CLICKHOUSE_PORT", "8123"))
CH_USER     = _env("EBUZIMA_CLICKHOUSE_USER", "admin")
CH_PASSWORD = _env("EBUZIMA_CLICKHOUSE_PASSWORD")
CH_DB       = _env("EBUZIMA_CLICKHOUSE_DB", "ebuzima")

_client = None

def get_client(force_new: bool = False):
    """Return (or create) a cached ClickHouse client.

    Pass force_new=True to discard a possibly-dead cached client and open a
    fresh connection — a pooled connection can go stale after a network blip
    and fail identically on every subsequent call otherwise.
    """
    global _client
    if force_new:
        _client = None
    if _client is not None:
        return _client
    if not CH_PASSWORD:
        raise RuntimeError("Set EBUZIMA_CLICKHOUSE_PASSWORD in .env")
    _client = clickhouse_connect.get_client(
        host=CH_HOST,
        port=CH_PORT,
        username=CH_USER,
        password=CH_PASSWORD,
        database=CH_DB,
    )
    return _client


def list_tables():
    """List every table in the configured ClickHouse database.

    Run this once to confirm the real table names for each DocType before
    trusting the TABLE_MAP defaults in ingest.py.
    """
    client = get_client()
    rows = client.query(f"SHOW TABLES FROM `{CH_DB}`").result_rows
    return [r[0] for r in rows]


def get_count(table: str, where: str | None = None) -> int:
    """Return row count for a table, optionally filtered."""
    client = get_client()
    sql = f"SELECT count() FROM `{CH_DB}`.`{table}`"
    if where:
        sql += f" WHERE {where}"
    return int(client.query(sql).result_rows[0][0])


def fetch_all(table: str, where: str | None = None, columns=None, retries: int = 3):
    """Fetch rows for a table as a pandas DataFrame.

    Retries up to `retries` times with backoff, forcing a fresh client
    connection between attempts (force_new=True) — a cached connection can go
    stale after a network blip and otherwise fail identically on every
    subsequent call for the rest of the process's life. Returns an empty
    DataFrame (not an exception) if every attempt fails, so ingest.py's
    per-doctype loop can continue past a bad table name/connection.
    """
    cols = ", ".join(f"`{c}`" for c in columns) if columns else "*"
    sql = f"SELECT {cols} FROM `{CH_DB}`.`{table}`"
    if where:
        sql += f" WHERE {where}"

    for attempt in range(retries):
        try:
            client = get_client(force_new=attempt > 0)
            return client.query_df(sql)
        except Exception as e:
            print(f"  [error] ClickHouse query failed for `{table}` "
                  f"(attempt {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)   # 1s → 2s

    import pandas as pd
    return pd.DataFrame()
