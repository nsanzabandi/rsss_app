"""
sync/etl.py — Pull data from eTracker API and upsert into local immunization_db.

Two modes:
  python sync/etl.py initial   → full load from START_DATE
  python sync/etl.py sync      → incremental from last_updated_on per district

Background usage (from Dash UI):
  from sync.etl import start_sync_job
  jid = start_sync_job(mode="sync")   # daemon thread, reports progress to jobs store
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# Make the project root importable when run as a script (python sync/etl.py …),
# so `from config...` works regardless of the current directory.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd
import psycopg2
import requests
from psycopg2.extras import execute_values

# Load .env so `python sync/etl.py` (run standalone) also picks up credentials.
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:
    pass

# ── Config (all from environment — never hardcode secrets) ─────────────────────

BASE_URL = os.environ.get("ETRACKER_URL", "https://tracker.moh.gov.rw")
AUTH     = (os.environ.get("ETRACKER_USER", ""),
            os.environ.get("ETRACKER_PASSWORD", ""))

# Programme / stage IDs (eTracker metadata for this surveillance programme)
PROGRAMME_ID = os.environ.get("ETRACKER_PROGRAMME_ID", "ESz6DeUp1Q9")
STAGE_ID     = os.environ.get("ETRACKER_STAGE_ID",     "Fha6aDys556")

DISTRICTS: dict[str, str] = {
    "XxBlJkEmJGQ": "Bugesera",   "QMTKhz1j2mA": "Nyagatare",
    "PnnZRLwoD66": "Rwamagana",  "fcW5X82FfpG": "Kayonza",
    "WOjncnBz0hi": "Gatsibo",    "ERRCgvW7La1": "Ngoma",
    "VqMwIodXtFZ": "Kirehe",     "fSyvbMUZWqJ": "Gasabo",
    "jqrJGsWovJs": "Nyarugenge", "rEmeA5Z7HcP": "Kicukiro",
    "jy5judMZtzS": "Nyamagabe",  "n95lDV3pgL5": "Ruhango",
    "MJ0JLxsTP70": "Nyanza",     "zuLjFsLTx2m": "Muhanga",
    "lU5vBlNgAW5": "Nyaruguru",  "MEqs8VG1Rx3": "Huye",
    "N9pKxz10nwa": "Gisagara",   "vb9Wtsjv0OS": "Kamonyi",
    "pXalpffB0lo": "Gakenke",    "BtzzCdcgFli": "Rulindo",
    "o5Gxx8zOilJ": "Gicumbi",    "rNmqHqUm4Cf": "Musanze",
    "bFXwg69YOeD": "Burera",     "yqapGWqiEra": "Rubavu",
    "DJKWdcLdPOI": "Karongi",    "DG8h5ijGxgO": "Rutsiro",
    "urGSAaskBqL": "Ngororero",  "PBHtCUM6nkg": "Nyamasheke",
    "ARCA1tta4rF": "Nyabihu",    "M6o8DrKq6P3": "Rusizi",
}

# Data element / attribute UIDs from eTracker
DIMENSIONS = [
    "ou:Hjw70Lodtf2",
    "jKahvk7wiQf", "e9BrRZAicPE", "sah5bll9Y6Q", "A31FfrjPqyp",
    "Rq4qM2wKYFL", "iCy9QPG9HY9", "QyhGvFhiMF3", "cM7Q2YcXhH0",
    "yXIS60L4z9a", "RzPmWr82V3p", "IBcgUcOzYgB", "cUTcjBma2jK",
    "YDWk8qQbFOh", "vBlHJN5gPSi", "uLotdmVESxa", "AClDcrZs6R3",
    "FBm5ftjnCj2", "getkVaLr2bw", "rXWNI9jQhXs", "fSGeXjMM7Iw",
    "oXj5h2R5fsF", "KCqOZmKogK0", "M5As5oDEfeu", "mBJQdNPOwPS",
    "PllkmKkrTIJ", "gDSP0BigErv", "QlVHGFnVkBE", "XuMqmrPOJgN",
    "TJaOcTK3Q45", "mGhcjKoNj2w", "rtcAoNWdVQP",
    "vV24RwmkGTs", "ZT2TUTwKrST", "K9zMbISYwAo", "QYNy3PnXcsu",
    "FdbDVxV7RRK", "AkKxLG1rfGG", "qdJiQuUx62C", "V6zlPQcfXIY",
]

# ── Job store (shared with jobs.py pattern) ────────────────────────────────────

_sync_jobs: dict[str, dict] = {}
_sync_lock = threading.Lock()

_print_lock = threading.Lock()
_db_write_lock = threading.Lock()


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    with _print_lock:
        print(f"[{ts}] [sync] {msg}")


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _get_conn():
    from config.db_local import get_local_conn
    return get_local_conn()


def create_table() -> None:
    """Create immunization_vaccination table if it doesn't exist."""
    conn = _get_conn()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS immunization_vaccination (
            event_id               TEXT PRIMARY KEY,
            entity_id              TEXT,
            child_id               TEXT,
            first_name             TEXT,
            family_name            TEXT,
            child_name             TEXT,
            date_of_birth          DATE,
            gender                 TEXT,
            place_of_birth         TEXT,
            twins                  TEXT,
            weight_at_birth_kg     NUMERIC,
            height_at_birth_cm     NUMERIC,
            mother_names           TEXT,
            mother_phone           TEXT,
            mother_dob             DATE,
            mother_education       TEXT,
            mother_id              TEXT,
            father_names           TEXT,
            father_phone           TEXT,
            residence_province     TEXT,
            residence_district     TEXT,
            residence_sector       TEXT,
            village                TEXT,
            health_facility        TEXT,
            h_district_hospital    TEXT,
            h_district             TEXT,
            h_sector               TEXT,
            h_cell                 TEXT,
            h_village              TEXT,
            facility_hierarchy     TEXT,
            last_immunization_date DATE,
            age_visit_years        NUMERIC,
            age_visit_months       NUMERIC,
            weight_visit_kg        NUMERIC,
            height_visit_cm        NUMERIC,
            muac_cm                TEXT,
            stunting_status        TEXT,
            wasting_status         TEXT,
            weight_status          TEXT,
            immunization_schedule  TEXT,
            next_visit_date        DATE,
            vaccination_site       TEXT,
            bcg                    TEXT,
            opv                    TEXT,
            hep_b_birth            TEXT,
            dpt_hepb_hib           TEXT,
            pneumococcal           TEXT,
            rotavirus              TEXT,
            measles_rubella        TEXT,
            hpv                    TEXT,
            district_source        TEXT,
            last_updated_on        TEXT,
            fetched_at             TIMESTAMP DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_iv_entity_id   ON immunization_vaccination(entity_id);
        CREATE INDEX IF NOT EXISTS idx_iv_district     ON immunization_vaccination(district_source);
        CREATE INDEX IF NOT EXISTS idx_iv_h_district   ON immunization_vaccination(h_district);
        CREATE INDEX IF NOT EXISTS idx_iv_h_hospital   ON immunization_vaccination(h_district_hospital);
        CREATE INDEX IF NOT EXISTS idx_iv_facility     ON immunization_vaccination(health_facility);
        CREATE INDEX IF NOT EXISTS idx_iv_imm_date     ON immunization_vaccination(last_immunization_date);
        CREATE INDEX IF NOT EXISTS idx_iv_stunting     ON immunization_vaccination(stunting_status);
        CREATE INDEX IF NOT EXISTS idx_iv_dob          ON immunization_vaccination(date_of_birth);

        -- Tracks per-page fetch progress for each (district, month-chunk) window
        -- of a fixed-window sync (initial / range / month). Lets a restarted
        -- sync resume from the next page instead of re-fetching a whole chunk
        -- (or whole district) from scratch. Not used by incremental ("sync")
        -- mode, whose per-district window already self-advances via
        -- last_updated_on.
        CREATE TABLE IF NOT EXISTS sync_progress (
            mode          TEXT NOT NULL,
            req_start     TEXT NOT NULL,
            req_end       TEXT NOT NULL,
            district      TEXT NOT NULL,
            chunk_start   TEXT NOT NULL,
            chunk_end     TEXT NOT NULL,
            last_page     INTEGER NOT NULL DEFAULT 0,
            done          BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at    TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (mode, req_start, req_end, district, chunk_start, chunk_end)
        );
        -- Upgrade path for installs that already have the table from before
        -- page-level tracking was added.
        ALTER TABLE sync_progress ADD COLUMN IF NOT EXISTS last_page  INTEGER   NOT NULL DEFAULT 0;
        ALTER TABLE sync_progress ADD COLUMN IF NOT EXISTS done       BOOLEAN   NOT NULL DEFAULT TRUE;
        ALTER TABLE sync_progress ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
    """)
    conn.commit()
    cur.close()
    conn.close()
    _log("Table ready")


# ── Resume tracking (fixed-window syncs: initial / range / month) ──────────────
# resume_key identifies "this exact requested sync" so a restart with the same
# parameters resumes instead of re-fetching everything. Format: (mode, req_start, req_end).

def _chunk_progress(resume_key: tuple[str, str, str] | None,
                    district_name: str, chunk_start: str, chunk_end: str) -> tuple[bool, int]:
    """Return (done, last_page_fetched) for this chunk. (False, 0) if never started."""
    if resume_key is None:
        return False, 0
    mode, req_start, req_end = resume_key
    try:
        conn = _get_conn()
        cur  = conn.cursor()
        cur.execute(
            "SELECT done, last_page FROM sync_progress WHERE mode=%s AND req_start=%s "
            "AND req_end=%s AND district=%s AND chunk_start=%s AND chunk_end=%s",
            (mode, req_start, req_end, district_name, chunk_start, chunk_end),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row is None:
            return False, 0
        return bool(row[0]), int(row[1])
    except Exception:
        return False, 0


def _save_chunk_progress(resume_key: tuple[str, str, str] | None,
                         district_name: str, chunk_start: str, chunk_end: str,
                         last_page: int, done: bool) -> None:
    if resume_key is None:
        return
    mode, req_start, req_end = resume_key
    try:
        conn = _get_conn()
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO sync_progress "
            "(mode, req_start, req_end, district, chunk_start, chunk_end, last_page, done, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW()) "
            "ON CONFLICT (mode, req_start, req_end, district, chunk_start, chunk_end) "
            "DO UPDATE SET last_page = EXCLUDED.last_page, done = EXCLUDED.done, "
            "updated_at = NOW()",
            (mode, req_start, req_end, district_name, chunk_start, chunk_end, last_page, done),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as exc:
        _log(f"  [resume] could not record progress for {district_name} "
             f"{chunk_start}→{chunk_end}: {exc}")


def clear_sync_progress(mode: str, req_start: str, req_end: str) -> int:
    """Forget resume progress for a given fixed-window sync, forcing the next
    run of that exact window to re-fetch every district from scratch."""
    conn = _get_conn()
    cur  = conn.cursor()
    cur.execute(
        "DELETE FROM sync_progress WHERE mode=%s AND req_start=%s AND req_end=%s",
        (mode, req_start, req_end),
    )
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return deleted


def _parse_hierarchy(hierarchy_str) -> dict:
    if not hierarchy_str or str(hierarchy_str).strip() == "":
        return {"h_district_hospital": None, "h_district": None,
                "h_sector": None, "h_cell": None, "h_village": None}
    parts = [p.strip() for p in str(hierarchy_str).split("/")]
    return {
        "h_district_hospital": parts[3] if len(parts) > 3 else None,
        "h_district":          parts[2] if len(parts) > 2 else None,
        "h_sector":            parts[4] if len(parts) > 4 else None,
        "h_cell":              parts[5] if len(parts) > 5 else None,
        "h_village":           parts[6] if len(parts) > 6 else None,
    }


def _clean(val) -> str | None:
    s = str(val).strip() if val is not None else ""
    return s if s not in ("", "None", "nan", "NaN") else None


def _upsert_rows(rows_df: pd.DataFrame, district_name: str) -> int:
    if rows_df.empty:
        return 0
    records = []
    for _, r in rows_df.iterrows():
        h    = _parse_hierarchy(r.get("Organisation unit name hierarchy"))
        first  = _clean(r.get("First Name"))  or ""
        family = _clean(r.get("Family Name")) or ""
        records.append((
            _clean(r.get("Event")),
            _clean(r.get("Tracked entity instance")),
            _clean(r.get("Auto Gen Child ID")),
            first  or None,
            family or None,
            (f"{first} {family}").strip() or None,
            _clean(r.get("Date of birth")),
            _clean(r.get("Gender")),
            _clean(r.get("Place of Bith")),
            _clean(r.get("Twins or More")),
            _clean(r.get("Weight at Birth (Kgs)")),
            _clean(r.get("Height at Birth(Cm)")),
            _clean(r.get("Mothers Names")),
            _clean(r.get("Mothers Phone Number")),
            _clean(r.get("Mothers Date of Birth")),
            _clean(r.get("Mothers Education Level")),
            _clean(r.get("Mothers ID number")),
            _clean(r.get("Father Names")),
            _clean(r.get("Father Phone Number")),
            _clean(r.get("Province of residence")),
            _clean(r.get("District of residence_vacc")),
            _clean(r.get("Sector of Residence")),
            _clean(r.get("Village of Residance")),
            _clean(r.get("Organisation unit name")),
            h["h_district_hospital"],
            h["h_district"],
            h["h_sector"],
            h["h_cell"],
            h["h_village"],
            _clean(r.get("Organisation unit name hierarchy")),
            _clean(r.get("Immunization date")),
            _clean(r.get("Age(s) at visit")),
            _clean(r.get("Month(s) at visit")),
            _clean(r.get("Weight at visit(Kgs)")),
            _clean(r.get("Height at visit(Cm)")),
            _clean(r.get("MUAC")),
            _clean(r.get("Nutrition status: Stunting")),
            _clean(r.get("Nutrition status: Wasting")),
            _clean(r.get("Nutrition status: Weight")),
            _clean(r.get("Immunization Schedule")),
            _clean(r.get("Next visit date")),
            _clean(r.get("Vaccination Site")),
            _clean(r.get("BCG")),
            _clean(r.get("OPV")),
            _clean(r.get("Hepatitis B at birth")),
            _clean(r.get("DPT-HepB-Hib")),
            _clean(r.get("Pneumococcal Vaccine")),
            _clean(r.get("Rotavirus vaccine")),
            _clean(r.get("Measles & Rubella (MR)")),
            _clean(r.get("Human Papilloma Virus Vaccine")),
            district_name,
            _clean(r.get("Last updated on")),
        ))

    sql = """
        INSERT INTO immunization_vaccination (
            event_id, entity_id, child_id, first_name, family_name, child_name,
            date_of_birth, gender, place_of_birth, twins,
            weight_at_birth_kg, height_at_birth_cm,
            mother_names, mother_phone, mother_dob, mother_education, mother_id,
            father_names, father_phone,
            residence_province, residence_district, residence_sector, village,
            health_facility,
            h_district_hospital, h_district, h_sector, h_cell, h_village,
            facility_hierarchy, last_immunization_date,
            age_visit_years, age_visit_months, weight_visit_kg, height_visit_cm,
            muac_cm, stunting_status, wasting_status, weight_status,
            immunization_schedule, next_visit_date, vaccination_site,
            bcg, opv, hep_b_birth, dpt_hepb_hib, pneumococcal,
            rotavirus, measles_rubella, hpv, district_source, last_updated_on
        ) VALUES %s
        ON CONFLICT (event_id) DO UPDATE SET
            entity_id              = EXCLUDED.entity_id,
            weight_visit_kg        = EXCLUDED.weight_visit_kg,
            height_visit_cm        = EXCLUDED.height_visit_cm,
            stunting_status        = EXCLUDED.stunting_status,
            wasting_status         = EXCLUDED.wasting_status,
            next_visit_date        = EXCLUDED.next_visit_date,
            immunization_schedule  = EXCLUDED.immunization_schedule,
            h_district_hospital    = EXCLUDED.h_district_hospital,
            h_district             = EXCLUDED.h_district,
            h_sector               = EXCLUDED.h_sector,
            last_updated_on        = EXCLUDED.last_updated_on,
            fetched_at             = NOW()
    """
    # Sort by event_id so every worker touches index tuples in the same order,
    # and serialize the write (with deadlock retry) so concurrent upserts to the
    # same table never deadlock.
    records.sort(key=lambda t: t[0] or "")
    for attempt in range(6):
        conn = _get_conn()
        cur  = conn.cursor()
        try:
            with _db_write_lock:
                execute_values(cur, sql, records)
                conn.commit()
            inserted = cur.rowcount
            cur.close(); conn.close()
            return inserted
        except psycopg2.errors.DeadlockDetected:
            try: conn.rollback()
            except Exception: pass
            cur.close(); conn.close()
            time.sleep(0.5 * (attempt + 1))
        except Exception:
            try: conn.rollback()
            except Exception: pass
            cur.close(); conn.close()
            raise
    _log(f"  {district_name}: upsert failed after retries (deadlock)")
    return 0


def _get_last_updated(district_name: str) -> str:
    """Return the most recent last_updated_on for a district, or a default."""
    try:
        conn = _get_conn()
        cur  = conn.cursor()
        cur.execute(
            "SELECT MAX(last_updated_on) FROM immunization_vaccination "
            "WHERE district_source = %s",
            (district_name,),
        )
        result = cur.fetchone()[0]
        cur.close()
        conn.close()
        return str(result)[:10] if result else "2024-07-01"
    except Exception:
        return "2024-07-01"


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    s = requests.Session()
    s.auth = AUTH
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=10, pool_maxsize=10,
        max_retries=requests.adapters.Retry(
            total=3, backoff_factor=2,
            status_forcelist=[500, 502, 503, 504],
        ),
    )
    s.mount("https://", adapter)
    return s


def _month_chunks(start_date: str, end_date: str) -> list[tuple[str, str]]:
    from dateutil.relativedelta import relativedelta
    chunks  = []
    current = datetime.strptime(start_date, "%Y-%m-%d")
    end     = datetime.strptime(end_date,   "%Y-%m-%d")
    while current < end:
        chunk_end = min(current + relativedelta(months=1), end)
        chunks.append((current.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        current = chunk_end
    return chunks


# ── District fetch ─────────────────────────────────────────────────────────────

def _fetch_district(
    district_id: str,
    district_name: str,
    start_date: str,
    end_date: str,
    progress_cb=None,   # optional callable(district_name, rows_so_far)
    resume_key: tuple[str, str, str] | None = None,
) -> tuple[str, int]:
    session          = _make_session()
    total_fetched    = 0
    all_chunks_done  = True   # stays True only if every chunk was skip-resumed
    chunks           = _month_chunks(start_date, end_date)

    for chunk_start, chunk_end in chunks:
        done, last_page = _chunk_progress(resume_key, district_name, chunk_start, chunk_end)
        if done:
            _log(f"  {district_name} [{chunk_start}] — already synced, skipping")
            continue
        all_chunks_done = False

        page = last_page + 1 if last_page else 1
        if last_page:
            _log(f"  {district_name} [{chunk_start}] — resuming from page {page} "
                 f"(page {last_page} already fetched)")
        chunk_ok = False   # only mark the chunk done on a clean finish, not an HTTP error
        while True:
            try:
                resp = session.get(
                    f"{BASE_URL}/api/analytics/events/query/{PROGRAMME_ID}",
                    params={
                        "stage":      STAGE_ID,
                        "dimension":  DIMENSIONS,
                        "filter":     f"ou:{district_id}",
                        "startDate":  chunk_start,
                        "endDate":    chunk_end,
                        "pageSize":   1000,
                        "page":       page,
                        "outputType": "EVENT",
                    },
                    timeout=120,
                )
                if resp.status_code != 200:
                    body = (resp.text or "")[:400].replace("\n", " ")
                    _log(f"  {district_name} {chunk_start} p{page}: "
                         f"HTTP {resp.status_code} — {body}")
                    break

                data    = resp.json()
                headers = [h["column"] for h in data.get("headers", [])]
                rows    = data.get("rows", [])
                if not rows:
                    chunk_ok = True   # legitimately no data for this window
                    break

                df = pd.DataFrame(rows, columns=headers)
                _upsert_rows(df, district_name)
                total_fetched += len(rows)

                if progress_cb:
                    progress_cb(district_name, total_fetched)

                total_pages = data.get("metaData", {}).get("pager", {}).get("pageCount", 1)
                _log(f"  {district_name} [{chunk_start}] p{page}/{total_pages} "
                     f"— {total_fetched:,} rows")

                # Persist after every successful page — a crash on page N+1
                # resumes at N+1, not from page 1 of the whole chunk.
                _save_chunk_progress(resume_key, district_name, chunk_start, chunk_end,
                                     last_page=page, done=False)

                if page >= total_pages:
                    chunk_ok = True   # all pages for this chunk fetched successfully
                    break
                page += 1
                time.sleep(0.1)

            except requests.exceptions.Timeout:
                _log(f"  {district_name} timeout — retrying in 15s")
                time.sleep(15)

            except requests.exceptions.ConnectionError as exc:
                _log(f"  {district_name} connection error: {exc} — retrying in 20s")
                time.sleep(20)
                session = _make_session()

        # Record this chunk as done only on a clean finish — an HTTP-error break
        # leaves it as "in progress at page N" (already saved above), so a
        # resumed run continues from page N+1 rather than the whole chunk.
        if chunk_ok:
            _save_chunk_progress(resume_key, district_name, chunk_start, chunk_end,
                                 last_page=page, done=True)

    if all_chunks_done and resume_key is not None:
        _log(f"  {district_name} DONE — already up to date (all chunks previously synced)")
    else:
        _log(f"  {district_name} DONE — {total_fetched:,} rows")
    return district_name, total_fetched


# ── Public sync functions ──────────────────────────────────────────────────────

def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def run_initial_load(start_date: str = "2025-07-01",
                     end_date:   str | None = None,
                     workers: int = 3,
                     resume: bool = True) -> dict:
    """Full load for all 30 districts. Runs synchronously (call in a thread).

    resume=True (default): if this exact date range was previously interrupted,
    districts/month-chunks already fully fetched are skipped. Pass resume=False
    to force a complete re-fetch, ignoring any prior progress for this range.
    """
    end_date = end_date or _today()
    create_table()
    resume_key = ("initial", start_date, end_date) if resume else None
    totals: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_fetch_district, did, dname, start_date, end_date,
                     None, resume_key): dname
            for did, dname in DISTRICTS.items()
        }
        for future in as_completed(futures):
            name, count = future.result()
            totals[name] = count
    return totals


def run_month_load(year: int, month: int, workers: int = 3,
                   resume: bool = True) -> dict:
    """
    (Re)fetch a single calendar month for ALL districts — e.g. run_month_load(2026, 6)
    to backfill June. Idempotent: rows upsert on event_id, so already-fetched
    events are updated in place, never duplicated. Use this when a month was
    missed (e.g. eTracker analytics weren't ready yet during the incremental sync).

    resume=True (default): if this exact month load was previously interrupted,
    districts already fully fetched are skipped rather than re-fetched.
    """
    from calendar import monthrange
    create_table()
    start = f"{year:04d}-{month:02d}-01"
    last  = monthrange(year, month)[1]
    end   = f"{year:04d}-{month:02d}-{last:02d}"
    _log(f"Month load {start} → {end} (all districts)")
    resume_key = ("month", start, end) if resume else None
    totals: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_fetch_district, did, dname, start, end,
                     None, resume_key): dname
            for did, dname in DISTRICTS.items()
        }
        for future in as_completed(futures):
            name, count = future.result()
            totals[name] = count
    return totals


def run_incremental_sync(workers: int = 3) -> dict:
    """Sync each district from its last known record date. Runs synchronously."""
    create_table()
    totals: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                _fetch_district, did, dname,
                _get_last_updated(dname), _today(),
            ): dname
            for did, dname in DISTRICTS.items()
        }
        for future in as_completed(futures):
            name, count = future.result()
            totals[name] = count
    return totals


# ── Background job API (used by Dash UI) ──────────────────────────────────────

def start_sync_job(mode: str = "sync",
                   start_date: str | None = None,
                   end_date: str | None = None,
                   resume: bool = True) -> str:
    """
    Launch a background sync thread and return a job ID.
    Poll with get_sync_job(jid) to track progress.
    mode: "initial" | "sync" | "range"
      • range → fetch a custom start_date..end_date window for all districts.

    resume=True (default): for "initial"/"range" (a fixed window shared by every
    district), if this exact window was previously interrupted, districts/chunks
    already fully fetched are skipped instead of re-fetched from scratch. Has no
    effect on "sync" mode, whose per-district window already self-advances via
    last_updated_on. Pass resume=False to force a full re-fetch of this window.
    """
    jid = str(uuid.uuid4())[:8]
    with _sync_lock:
        _sync_jobs[jid] = {
            "id":       jid,
            "mode":     mode,
            "status":   "running",
            "started":  datetime.now().isoformat(),
            "progress": {},  # {district_name: rows}
            "totals":   {},
            "error":    None,
        }

    def _worker():
        try:
            _log(f"Sync job {jid} started (mode={mode})")
            create_table()

            def _cb(district_name, rows):
                with _sync_lock:
                    _sync_jobs[jid]["progress"][district_name] = rows

            def _window(dname):
                if mode == "initial":
                    return "2025-07-01", _today()
                if mode == "range":
                    return (start_date or "2025-07-01"), (end_date or _today())
                return _get_last_updated(dname), _today()

            # Fixed-window modes (initial/range) share the same requested window
            # across every district, so resume tracking applies. "sync" mode's
            # window differs per district (from last_updated_on) and is already
            # naturally incremental — no resume_key needed there.
            resume_key = (mode, *_window(None)) if resume and mode in ("initial", "range") else None

            results: dict[str, int] = {}
            with ThreadPoolExecutor(max_workers=3) as ex:
                futures = {}
                for did, dname in DISTRICTS.items():
                    s, e = _window(dname)
                    futures[ex.submit(_fetch_district, did, dname, s, e,
                                      _cb, resume_key)] = dname
                for future in as_completed(futures):
                    name, count = future.result()
                    results[name] = count
                    with _sync_lock:
                        _sync_jobs[jid]["totals"] = results

            with _sync_lock:
                _sync_jobs[jid]["status"] = "completed"
                _sync_jobs[jid]["totals"] = results
            _log(f"Sync job {jid} complete — {sum(results.values()):,} total rows")

            # Refresh dashboard caches so new data shows without a restart.
            try:
                import data
                data.invalidate_cache()
            except Exception:
                pass

        except Exception as exc:
            with _sync_lock:
                _sync_jobs[jid]["status"] = "failed"
                _sync_jobs[jid]["error"]  = str(exc)
            _log(f"Sync job {jid} FAILED: {exc}")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return jid


def get_sync_job(jid: str) -> dict | None:
    with _sync_lock:
        return _sync_jobs.get(jid)


# ── CLI entry-point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if mode == "initial":
        start = sys.argv[2] if len(sys.argv) > 2 else "2025-07-01"
        end   = sys.argv[3] if len(sys.argv) > 3 else _today()
        _log(f"Initial load {start} → {end}")
        totals = run_initial_load(start, end)
    elif mode == "month":
        # python sync/etl.py month 2026 6   → (re)fetch June 2026, dedup-safe
        if len(sys.argv) < 4:
            _log("Usage: python sync/etl.py month <year> <month>   e.g. month 2026 6")
            raise SystemExit(1)
        yr, mo = int(sys.argv[2]), int(sys.argv[3])
        totals = run_month_load(yr, mo)
    else:
        _log("Incremental sync")
        totals = run_incremental_sync()

    _log("Results:")
    for d, n in sorted(totals.items()):
        _log(f"  {d:<20} {n:>8,} rows")
    _log(f"  {'TOTAL':<20} {sum(totals.values()):>8,} rows")
