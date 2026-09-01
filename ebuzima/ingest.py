import argparse
import time

import pandas as pd

from .clickhouse_source import fetch_all
from .db import get_engine, get_last_modified, hash_pii, list_tables, save, upsert

# ── Target DocTypes ──────────────────────────────────────────────────────────
TARGET_DOCTYPES = [
    "Nutrition",
    "Nutrition Distribution",
    "Nutrition Followup",
    "Nutrition Met Criteria",
    "Nutrition Referral",
    "Nutrition State",
    "Fathers Additional Partner Children Detail",
    "Mothers Additional Partner Children Detail",
    "Child Growth Monitoring",
    "Child Growth Monitoring Followup",
]

# ── DocType → ClickHouse table name ──────────────────────────────────────────
# ASSUMPTION: ClickHouse mirrors Frappe's own `tabDocType` naming convention
# (this is the standard pattern for Frappe → ClickHouse CDC replication).
# Run `python -m ebuzima.check_clickhouse_tables` to confirm real names, then
# edit below if they differ (e.g. no "tab" prefix, underscores instead of spaces).
TABLE_MAP = {dt: f"tab{dt}" for dt in TARGET_DOCTYPES}


def table_name(doctype: str) -> str:
    """Convert DocType name to a safe Postgres table name (unchanged — this
    is the destination table name, not the ClickHouse source table name)."""
    return doctype.lower().replace(" ", "_")


def build_where(start=None, end=None, field="creation"):
    """Build a SQL WHERE clause from an optional date range."""
    conds = []
    if start:
        conds.append(f"`{field}` >= '{start}'")
    if end:
        conds.append(f"`{field}` < '{end}'")
    return " AND ".join(conds) if conds else None


def ingest_doctype(doctype: str, where: str = None, incremental: bool = True) -> dict:
    """Fetch one DocType from ClickHouse, mask PII, upsert to PostgreSQL.

    In incremental mode (default), only records modified since the last sync
    are fetched. Pass incremental=False or --full on CLI to force a complete
    re-fetch.

    Returns a summary dict: doctype, table, fetched, inserted, updated, stored.
    """
    tbl      = table_name(doctype)
    ch_table = TABLE_MAP.get(doctype, doctype)
    print(f"\n{'─'*55}")
    print(f"  DocType     : {doctype}")
    print(f"  CH table    : {ch_table}")
    print(f"  PG table    : {tbl}")

    # ── Incremental filter ────────────────────────────────────────────────────
    active_where = where
    if incremental and where is None:
        last_mod = get_last_modified(tbl)
        if last_mod:
            active_where = f"modified > '{last_mod}'"
            print(f"  Mode        : incremental (modified > {last_mod})")
        else:
            print(f"  Mode        : full (first sync or empty table)")

    # ── Fetch from ClickHouse ────────────────────────────────────────────────
    t0 = time.time()
    df = fetch_all(ch_table, where=active_where)
    elapsed = time.time() - t0

    if df is None or df.empty:
        print(f"  Fetched 0 rows")
        return {"doctype": doctype, "table": tbl, "fetched": 0, "inserted": 0, "updated": 0, "stored": 0}

    df = hash_pii(df)
    print(f"  Fetched {len(df):,} rows × {len(df.columns)} cols in {elapsed:.1f}s")

    # ── DB upsert ────────────────────────────────────────────────────────────
    inserted = updated = stored = 0
    engine = get_engine()
    if engine is None:
        print("  [warn] eBuzima DB not configured — rows not stored (set EBUZIMA_DB_USER/EBUZIMA_DB_PASSWORD/EBUZIMA_DB_NAME in .env)")
    else:
        existing = list_tables()
        try:
            if tbl in existing and "name" in df.columns:
                counts = upsert(df, tbl, key="name")
                inserted = counts.get("inserted", 0)
                updated  = counts.get("updated",  0)
                stored   = inserted + updated
                print(f"  DB          : {inserted:,} inserted, {updated:,} updated → {tbl}")
            else:
                save(df, tbl, if_exists="replace")
                inserted = len(df)
                stored   = inserted
                print(f"  DB          : {inserted:,} inserted (full load) → {tbl}")
        except Exception as e:
            print(f"  [error] DB persist failed: {e}")

    return {
        "doctype": doctype,
        "table":   tbl,
        "fetched": len(df),
        "inserted": inserted,   # truly new records
        "updated":  updated,    # existing records refreshed
        "stored":   stored,     # inserted + updated
    }


def main():
    parser = argparse.ArgumentParser(description="Ingest eBuzima nutrition data from ClickHouse into PostgreSQL")
    parser.add_argument("-d", "--doctype", help="Single DocType to ingest (default: all)")
    parser.add_argument("--full", action="store_true",
                        help="Force full re-sync (default: incremental — only new/modified rows)")
    parser.add_argument("--start", help="Filter by date >= this value (YYYY-MM-DD)")
    parser.add_argument("--end",   help="Filter by date <  this value (YYYY-MM-DD)")
    parser.add_argument("--date-field", default="creation",
                        help="Date field for --start/--end filter (default: creation)")
    args = parser.parse_args()

    doctypes    = [args.doctype] if args.doctype else TARGET_DOCTYPES
    where       = build_where(args.start, args.end, args.date_field)
    # incremental = True unless --full is set or an explicit date range is provided
    incremental = not args.full and not (args.start or args.end)

    if where:
        print(f"Date filter  : {where}")
    elif not incremental:
        print("Mode         : full re-sync (--full)")
    else:
        print("Mode         : incremental — only new/modified rows  (use --full to re-sync all)")

    results = []
    for dt in doctypes:
        try:
            results.append(ingest_doctype(dt, where=where, incremental=incremental))
        except Exception as e:
            print(f"\n[error] {dt} failed: {e}")
            results.append({"doctype": dt, "table": table_name(dt),
                            "fetched": 0, "inserted": 0, "updated": 0, "stored": 0})

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'='*55}")
    print("INGESTION SUMMARY")
    print(f"{'='*55}")
    summary_df = pd.DataFrame(results)[["doctype", "fetched", "inserted", "updated", "stored"]]
    print(summary_df.to_string(index=False))
    total_fetched  = sum(r["fetched"]   for r in results)
    total_inserted = sum(r["inserted"]  for r in results)
    total_updated  = sum(r["updated"]   for r in results)
    print(f"\nFetched: {total_fetched:,}  |  Inserted: {total_inserted:,}  |  Updated: {total_updated:,}")


if __name__ == "__main__":
    main()
