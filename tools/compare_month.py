"""
tools/compare_month.py — For one month, compare how many events eTracker
holds with how many RSSS has stored, per district. Read-only on both sides.

Usage (from the project root, or inside the app container):
    python -m tools.compare_month 2025 6

A district far below eTracker's count was not fully synced — reload that
month with:
    python -c "from sync.etl import run_month_load; run_month_load(2025, 6)"
If eTracker's own national total looks far too small, check the eTracker
account's data access (or eTracker's analytics tables) before reloading.
"""
from __future__ import annotations

import sys
from calendar import monthrange
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _etracker_total(session, ou: str, start: str, end: str):
    import sync.etl as E
    r = session.get(f"{E.BASE_URL}/api/analytics/events/query/{E.PROGRAMME_ID}",
                    params={"stage": E.STAGE_ID, "dimension": E.DIMENSIONS + [f"ou:{ou}"],
                            "startDate": start, "endDate": end, "pageSize": 1, "page": 1,
                            "totalPages": "true", "outputType": "EVENT", "timeField": "EVENT_DATE"},
                    timeout=300)
    if r.status_code != 200:
        return None
    return r.json().get("metaData", {}).get("pager", {}).get("total")


def main(year: int, month: int) -> None:
    import sync.etl as E
    from config.db_local import get_local_conn
    start = f"{year:04d}-{month:02d}-01"
    end = f"{year:04d}-{month:02d}-{monthrange(year, month)[1]:02d}"
    session = E._make_session()
    conn = get_local_conn(); cur = conn.cursor()
    # Count stored visits by the facility's district (h_district, e.g. "Gasabo District").
    cur.execute("""SELECT h_district, COUNT(*) FROM immunization_vaccination
                   WHERE last_immunization_date BETWEEN %s AND %s GROUP BY 1""", (start, end))
    stored = {str(d or "").replace(" District", "").strip(): n for d, n in cur.fetchall()}
    cur.close(); conn.close()

    print(f"{start} → {end}")
    print(f"{'district':12} {'eTracker':>9} {'RSSS':>9} {'RSSS %':>7}")
    tot_e = tot_s = 0
    for ou, name in sorted(E.DISTRICTS.items(), key=lambda kv: kv[1]):
        e = _etracker_total(session, ou, start, end)
        s = stored.get(name, 0)
        tot_s += s
        if e is None:
            print(f"{name:12} {'error':>9} {s:>9,}")
            continue
        tot_e += e
        pct = f"{s / e:.0%}" if e else "—"
        flag = "  ← incomplete" if e and s < 0.9 * e else ""
        print(f"{name:12} {e:>9,} {s:>9,} {pct:>7}{flag}")
    print(f"{'TOTAL':12} {tot_e:>9,} {tot_s:>9,} {(tot_s / tot_e if tot_e else 0):>7.0%}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    if len(sys.argv) != 3:
        print(__doc__); sys.exit(1)
    main(int(sys.argv[1]), int(sys.argv[2]))
