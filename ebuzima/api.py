"""eBuzima Frappe API client.

Legacy/fallback path — the primary data path is clickhouse_source.py + ingest.py.
This is kept for the "API status" health-check ping on the eBuzima page, and as
a standalone fallback (_ingest_csv.py) if ClickHouse access is ever unavailable.

Provides:
- get_session()  — thread-local requests.Session with token auth
- get_count()    — total record count for a DocType
- fetch_page()   — single paginated page (3 retries, exponential backoff)
- fetch_all()    — all records for a DocType (sequential pagination)
"""
import os
import time
import threading
from dotenv import load_dotenv

load_dotenv()   # override=False (default) — never clobber env vars set outside .env
import requests


def _env(key, default=None):
    v = os.getenv(key, default)
    return v.strip().strip('"').strip("'") if v else default


BASE_URL = _env("EBUZIMA_BASE_URL", "https://ebuzima.moh.gov.rw")
API_KEY = _env("EBUZIMA_API_KEY")
API_SECRET = _env("EBUZIMA_API_SECRET")
EBUZIMA_USER = _env("EBUZIMA_USER")
EBUZIMA_PASSWORD = _env("EBUZIMA_PASSWORD")

_local = threading.local()


def get_session():
    """Return a thread-local requests.Session authenticated with API token or credentials."""
    if not hasattr(_local, "s"):
        s = requests.Session()
        s.headers["Content-Type"] = "application/json"
        
        if EBUZIMA_USER and EBUZIMA_PASSWORD:
            resp = s.post(
                f"{BASE_URL}/api/method/login",
                json={"usr": EBUZIMA_USER, "pwd": EBUZIMA_PASSWORD},
                timeout=30
            )
            if resp.status_code != 200:
                raise RuntimeError(f"eBuzima login failed: HTTP {resp.status_code} - {resp.text}")
        elif API_KEY and API_SECRET:
            s.headers["Authorization"] = f"token {API_KEY}:{API_SECRET}"
        else:
            raise RuntimeError(
                "Set EBUZIMA_USER and EBUZIMA_PASSWORD (or EBUZIMA_API_KEY and SECRET) in .env"
            )
            
        _local.s = s
    return _local.s


def get_count(doctype, filters=None):
    """Return total record count for a DocType."""
    payload = {"doctype": doctype}
    if filters:
        payload["filters"] = filters
    r = get_session().post(
        f"{BASE_URL}/api/method/frappe.client.get_count",
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    return int(r.json().get("message", 0))


# Sentinel returned by fetch_page() when the server returns 403 (permission denied).
# fetch_all() detects this and aborts immediately — no point retrying remaining pages.
_PERMISSION_DENIED = object()


def fetch_page(doctype, offset=0, limit=500, filters=None, fields=None):
    """Fetch one page of records. Returns list of dicts.

    Returns _PERMISSION_DENIED sentinel on HTTP 403 (caller should abort).
    Retries up to 3 times with exponential backoff (1s → 4s → 16s) for other errors.
    """
    payload = {
        "doctype": doctype,
        "limit_start": offset,
        "limit_page_length": limit,
        "fields": fields or ["*"],
    }
    if filters:
        payload["filters"] = filters

    for attempt in range(3):
        try:
            r = get_session().post(
                f"{BASE_URL}/api/method/frappe.client.get_list",
                json=payload,
                timeout=120,
            )
            if r.status_code == 200:
                return r.json().get("message", [])
            if r.status_code == 403:
                # Permission denied — retrying won't help; signal caller to abort
                print(f"  [403] {doctype}: API key lacks read permission for this DocType.")
                print(f"        → On the eBuzima Frappe server, grant 'read' access to")
                print(f"          '{doctype}' for the role assigned to your API key.")
                return _PERMISSION_DENIED
            # Other non-200: log and back off before retry
            print(f"  [warn] HTTP {r.status_code} at offset={offset} "
                  f"for {doctype} (attempt {attempt+1}/3)")
            time.sleep(4 ** attempt)   # 1s → 4s → 16s
        except Exception as e:
            print(f"  [warn] {type(e).__name__} at offset={offset} "
                  f"for {doctype} (attempt {attempt+1}/3): {e}")
            time.sleep(4 ** attempt)

    print(f"  [warn] page at offset={offset} permanently failed for {doctype}")
    return []


def fetch_all(doctype, filters=None, fields=None, page_size=500):
    """Fetch every record for a DocType using sequential pagination.

    Returns a list of dicts, or [] if no records / permission denied.
    """
    total = get_count(doctype, filters)
    if total == 0:
        print(f"  {doctype}: 0 records — skipping")
        return []

    print(f"  {doctype}: {total:,} records to fetch")
    rows = []
    for offset in range(0, total, page_size):
        batch = fetch_page(doctype, offset, page_size, filters, fields)
        if batch is _PERMISSION_DENIED:
            print(f"  [skip] Aborting {doctype} — permission denied (HTTP 403)")
            return []   # fast exit; no retries for remaining pages
        rows.extend(batch)
        pct = int(len(rows) / total * 100)
        if offset % (page_size * 10) == 0 and offset > 0:
            print(f"    {len(rows):,} / {total:,}  ({pct}%)")
        time.sleep(0.3)  # gentle pacing — avoids rate-limiting on large DocTypes

    print(f"  Done — {len(rows):,} rows fetched")
    return rows
