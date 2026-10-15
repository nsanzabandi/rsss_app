"""
tools/match_villages.py — Build the village_match lookup table: each distinct
home address recorded in eTracker (district code, sector, village) → the
official village (core/village_matcher.py). eTracker's own columns are never
changed; downloads and report case lists join to this table.

Usage (from the project root):
    python -m tools.match_villages            # match new addresses only (fast)
    python -m tools.match_villages --full     # re-match everything (after the
                                              # reference list or overrides change)
    python -m tools.match_villages --review reports/village_review.csv
                                              # addresses needing a person's check,
                                              # most-used first, with suggestions

It also runs automatically after each eTracker sync (data.refresh_after_sync).
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_DDL = """
CREATE TABLE IF NOT EXISTS village_match (
    district_raw  TEXT NOT NULL,
    sector_raw    TEXT NOT NULL,
    village_raw   TEXT NOT NULL,
    records       INTEGER,
    status        TEXT NOT NULL,
    village_id    TEXT,
    village       TEXT,
    cell          TEXT,
    sector        TEXT,
    district      TEXT,
    score         REAL,
    matched_at    TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (district_raw, sector_raw, village_raw)
);
"""

# For queries on immunization_vaccination aliased iv: the join (raw values
# exactly as stored, NULL → '') and the columns to select.
JOIN_SQL = ("LEFT JOIN village_match vm ON vm.district_raw = COALESCE(iv.residence_district, '') "
            "AND vm.sector_raw = COALESCE(iv.residence_sector, '') "
            "AND vm.village_raw = COALESCE(iv.village, '')")
VILLAGE_COLS_SQL = """
       COALESCE(vm.village, iv.village) AS village, vm.cell AS cell,
       CASE WHEN vm.status IN ('verified', 'override') THEN 'Verified'
            WHEN vm.status = 'verified_cell_unknown' THEN 'Verified (cell not known)'
            WHEN vm.status = 'verified_fuzzy' THEN 'Close spelling - please confirm'
            WHEN vm.status = 'missing' THEN 'No village recorded'
            WHEN vm.status IS NULL THEN ''
            ELSE 'Not verified' END AS village_check"""

APPLIED = ("verified", "verified_fuzzy", "verified_cell_unknown", "override")


def ensure_table(cur) -> None:
    cur.execute(_DDL)


def run(full: bool = False, verbose: bool = True) -> dict:
    from config.db_local import get_local_conn
    from core.village_matcher import load_overrides, match, reference

    t0 = time.time()
    ref = reference()
    overrides = load_overrides()
    conn = get_local_conn(); cur = conn.cursor()
    ensure_table(cur)
    if full:
        cur.execute("DELETE FROM village_match")
    cur.execute("""
        SELECT COALESCE(residence_district, ''), COALESCE(residence_sector, ''),
               COALESCE(village, ''), COUNT(*)
        FROM immunization_vaccination GROUP BY 1, 2, 3""")
    combos = cur.fetchall()
    cur.execute("SELECT district_raw, sector_raw, village_raw FROM village_match")
    known = set(cur.fetchall())

    new_rows, counts = [], []
    for d, s, v, n in combos:
        counts.append((n, d, s, v))
        if (d, s, v) in known:
            continue
        m = match(d, s, v, overrides)
        r = ref.villages.get(m["village_id"] or "")
        new_rows.append((d, s, v, n, m["status"], m["village_id"],
                         r["village"] if r else None,
                         r["cell"] if r and m["status"] != "verified_cell_unknown" else None,
                         r["sector"] if r else None, r["district"] if r else None,
                         round(m["score"], 3)))
    if new_rows:
        from psycopg2.extras import execute_values
        execute_values(cur, """
            INSERT INTO village_match (district_raw, sector_raw, village_raw, records, status,
                                       village_id, village, cell, sector, district, score)
            VALUES %s ON CONFLICT DO NOTHING""", new_rows, page_size=5000)
    # refresh record counts (cheap) so the review list is ordered by current use
    from psycopg2.extras import execute_values
    cur.execute("CREATE TEMP TABLE _vm_counts (records INT, d TEXT, s TEXT, v TEXT) ON COMMIT DROP")
    execute_values(cur, "INSERT INTO _vm_counts VALUES %s", counts, page_size=10000)
    cur.execute("""UPDATE village_match vm SET records = c.records FROM _vm_counts c
                   WHERE vm.district_raw = c.d AND vm.sector_raw = c.s AND vm.village_raw = c.v""")
    cur.execute("""SELECT status, COUNT(*), SUM(records) FROM village_match
                   GROUP BY status ORDER BY 3 DESC""")
    summary = {st: (int(n), int(rec or 0)) for st, n, rec in cur.fetchall()}
    conn.commit(); cur.close(); conn.close()
    if verbose:
        total = sum(r for _, r in summary.values()) or 1
        print(f"[villages] {len(new_rows):,} new addresses matched in {time.time()-t0:.0f}s")
        for st, (n, rec) in summary.items():
            print(f"  {st:24s} {n:>8,} spellings  {rec:>10,} visits  {rec/total:6.1%}")
    return summary


def write_review(path: str, limit: int = 5000) -> int:
    """Addresses that need a person's check, most-used first, with suggestions.
    Fill in village_id (from the suggestion in [brackets], or
    config/rwanda_villages.csv) and copy the row into config/village_overrides.csv."""
    from config.db_local import get_local_conn
    from core.village_matcher import suggestion
    conn = get_local_conn(); cur = conn.cursor()
    cur.execute("""SELECT district_raw, sector_raw, village_raw, records, status
                   FROM village_match WHERE status IN ('check', 'not_found')
                   ORDER BY records DESC LIMIT %s""", (limit,))
    rows = cur.fetchall(); cur.close(); conn.close()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["district_raw", "sector_raw", "village_raw", "records", "status",
                    "suggestions", "village_id"])
        for d, s, v, n, st in rows:
            w.writerow([d, s, v, n, st, suggestion(d, s, v), ""])
    print(f"[villages] review list: {len(rows):,} addresses → {path}")
    return len(rows)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    args = sys.argv[1:]
    if "--review" in args:
        i = args.index("--review")
        write_review(args[i + 1] if len(args) > i + 1 else "reports/village_review.csv")
    else:
        run(full="--full" in args)
