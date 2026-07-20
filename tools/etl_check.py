"""
tools/etl_check.py — Diagnose why eTracker → immunization_db is empty.

Run from the project root (venv active):
    python tools/etl_check.py

Checks, in order:
  1. Local Postgres reachable?
  2. immunization_vaccination table exists? row count? distinct children?
  3. eTracker API reachable + credentials valid?
  4. A real 1-month sample fetch for one district returns rows?

Each step prints a clear PASS / FAIL so you can see exactly where the
pipeline breaks instead of silently getting 0 rows.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")
    print("─" * 60)


def check_db() -> bool:
    step(1, "Local Postgres (immunization_db) reachable?")
    try:
        from config.db_local import get_local_conn, local_db
        print(f"  DSN: host={local_db.HOST} port={local_db.PORT} "
              f"dbname={local_db.NAME} user={local_db.USER}")
        conn = get_local_conn()
        conn.close()
        print("  ✅ PASS — connection succeeded.")
        return True
    except Exception as exc:
        print(f"  ❌ FAIL — {exc}")
        print("  → Is Postgres running? Does the 'immunization_db' database exist?")
        print("    Create it with:  createdb immunization_db")
        return False


def check_table() -> None:
    step(2, "immunization_vaccination table contents")
    try:
        from config.db_local import get_local_conn
        conn = get_local_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'immunization_vaccination'
            )
        """)
        exists = cur.fetchone()[0]
        if not exists:
            print("  ⚠ Table does not exist yet — it is created on first sync.")
            cur.close(); conn.close()
            return
        cur.execute("SELECT COUNT(*) FROM immunization_vaccination")
        rows = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT entity_id) FROM immunization_vaccination")
        kids = cur.fetchone()[0]
        cur.execute("SELECT MIN(last_immunization_date), MAX(last_immunization_date) "
                    "FROM immunization_vaccination")
        lo, hi = cur.fetchone()
        print(f"  rows (events)      : {rows:,}")
        print(f"  distinct children  : {kids:,}")
        print(f"  immunization dates : {lo} → {hi}")
        if rows == 0:
            print("  ⚠ Table is EMPTY — run a sync (steps 3–4 show whether eTracker works).")
        else:
            print("  ✅ PASS — table has data.")
            # Show stunting_status encoding + how many rows the dashboard filter matches.
            cur.execute("""
                SELECT stunting_status, COUNT(*) n
                FROM immunization_vaccination
                GROUP BY stunting_status ORDER BY n DESC LIMIT 12
            """)
            print("  stunting_status values:")
            for v, n in cur.fetchall():
                print(f"     {v!r}: {n:,}")
            cur.execute("""
                SELECT COUNT(*) FROM immunization_vaccination
                WHERE stunting_status IS NOT NULL
                  AND LOWER(TRIM(stunting_status)) LIKE '%stunt%'
                  AND LOWER(TRIM(stunting_status)) NOT LIKE 'not %'
                  AND LOWER(TRIM(stunting_status)) NOT LIKE 'no %'
            """)
            matched = cur.fetchone()[0]
            print(f"  → dashboard stunted filter matches: {matched:,} rows")
            if matched == 0:
                print("    ⚠ 0 matches — the values above don't contain 'stunt'. "
                      "Tell me the real values and I'll map the filter to them.")
        cur.close(); conn.close()
    except Exception as exc:
        print(f"  ❌ FAIL — {exc}")


def check_api() -> bool:
    step(3, "eTracker API reachable + credentials valid?")
    try:
        import requests
        from sync.etl import BASE_URL, AUTH
        url = f"{BASE_URL}/api/me"
        print(f"  GET {url}  as {AUTH[0]}")
        r = requests.get(url, auth=AUTH, timeout=30)
        print(f"  HTTP {r.status_code}")
        if r.status_code == 200:
            print("  ✅ PASS — authenticated.")
            return True
        if r.status_code in (401, 403):
            print("  ❌ FAIL — credentials rejected (check AUTH in sync/etl.py).")
        else:
            print("  ❌ FAIL — unexpected status.")
        return False
    except Exception as exc:
        print(f"  ❌ FAIL — {exc} (network / VPN / host reachable?)")
        return False


def check_sample() -> None:
    step(4, "Sample fetch — 1 district, last 1 month (analytics endpoint)")
    did = dname = None
    start = end = None
    try:
        from sync.etl import DISTRICTS, _fetch_district, _today
        from dateutil.relativedelta import relativedelta
        did, dname = next(iter(DISTRICTS.items()))
        start = (datetime.now() - relativedelta(months=1)).strftime("%Y-%m-%d")
        end = _today()
        print(f"  District: {dname}   {start} → {end}")
        name, n = _fetch_district(did, dname, start, end)
        if n > 0:
            print(f"  ✅ PASS — fetched {n:,} rows (also upserted into the DB).")
        else:
            print("  ⚠ analytics returned 0 rows (see the HTTP line above for the "
                  "exact reason — a 409 usually means analytics tables are not yet "
                  "generated for this recent period).")
    except Exception as exc:
        print(f"  ❌ FAIL — {exc}")

    # Probe the LIVE events endpoint for the same window. If this returns data
    # while analytics 409s, the fix is to pull recent months from /api/events.
    step(5, "Live events endpoint probe (does recent data exist there?)")
    if not did:
        print("  (skipped — no district)")
        return
    try:
        import requests
        from sync.etl import BASE_URL, AUTH, PROGRAMME_ID
        r = requests.get(
            f"{BASE_URL}/api/events.json",
            auth=AUTH,
            params={
                "program": PROGRAMME_ID,
                "orgUnit": did,
                "ouMode": "DESCENDANTS",
                "startDate": start,
                "endDate": end,
                "pageSize": 1,
                "totalPages": "true",
            },
            timeout=60,
        )
        print(f"  HTTP {r.status_code}")
        if r.status_code == 200:
            j = r.json()
            total = j.get("pager", {}).get("total")
            got = len(j.get("events", j.get("instances", [])))
            print(f"  pager.total = {total}   (returned {got} in this probe)")
            if total:
                print("  ✅ Live events DO contain data for this window.")
                print("  → Recommended: pull recent months from /api/events and run")
                print("    DHIS2 analytics nightly so the analytics endpoint catches up.")
            else:
                print("  ⚠ Events endpoint also empty for this window — the data may "
                      "simply not be entered yet for these dates.")
        else:
            print(f"  body: {(r.text or '')[:300]}")
    except Exception as exc:
        print(f"  ❌ probe failed — {exc}")


def main() -> int:
    print("=" * 60)
    print("  RSSS ETL diagnostic")
    print("=" * 60)
    db_ok = check_db()
    if db_ok:
        check_table()
    api_ok = check_api()
    if api_ok and db_ok:
        check_sample()
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
