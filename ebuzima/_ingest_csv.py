"""Standalone ingestion runner — no sqlalchemy required.

Fetches all 9 DocTypes from the eBuzima Frappe API and saves each to
data/exports/<table_name>.csv. Also writes data/exports/manifest.csv.

Fallback path only — not wired into pages/ebuzima.py. The primary data path
is clickhouse_source.py + ingest.py (ClickHouse → PostgreSQL).
"""
import hashlib
import os
import time
import json

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()   # override=False (default) — never clobber env vars set outside .env

def _env(k, default=None):
    v = os.getenv(k, default)
    return v.strip().strip('"').strip("'") if v else default

BASE_URL   = _env("EBUZIMA_BASE_URL", "https://ebuzima.moh.gov.rw")
API_KEY    = _env("EBUZIMA_API_KEY")
API_SECRET = _env("EBUZIMA_API_SECRET")

TARGET_DOCTYPES = [
    "Nutrition",
    "Nutrition Distribution",
    "Nutrition Followup",
    "Nutrition Met Criteria",
    "Nutrition Referral",
    "Nutrition State",
    "Additional Partner Children Detail",
    "Child Growth Monitoring",
    "Child Growth Monitoring Followup",
]

_DEFAULT_PII_COLS = (
    "patient_name","national_id","nhis","phone_number","phone",
    "email","id_number","father_name","mother_name",
)

_PERMISSION_DENIED = object()


EBUZIMA_USER = _env("EBUZIMA_USER")
EBUZIMA_PASSWORD = _env("EBUZIMA_PASSWORD")

def _session():
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
        raise RuntimeError("Set EBUZIMA_USER and EBUZIMA_PASSWORD (or EBUZIMA_API_KEY/SECRET) in .env")
        
    return s


def get_count(sess, doctype, filters=None):
    payload = {"doctype": doctype}
    if filters:
        payload["filters"] = filters
    r = sess.post(f"{BASE_URL}/api/method/frappe.client.get_count",
                  json=payload, timeout=60)
    r.raise_for_status()
    return int(r.json().get("message", 0))


def fetch_page(sess, doctype, offset=0, limit=500, filters=None):
    payload = {"doctype": doctype, "limit_start": offset,
               "limit_page_length": limit, "fields": ["*"]}
    if filters:
        payload["filters"] = filters
    for attempt in range(3):
        try:
            r = sess.post(f"{BASE_URL}/api/method/frappe.client.get_list",
                          json=payload, timeout=120)
            if r.status_code == 200:
                return r.json().get("message", [])
            if r.status_code == 403:
                print(f"  [403] {doctype}: permission denied — skipping")
                return _PERMISSION_DENIED
            print(f"  [warn] HTTP {r.status_code} offset={offset} attempt {attempt+1}/3")
            time.sleep(4 ** attempt)
        except Exception as e:
            print(f"  [warn] {type(e).__name__} offset={offset} attempt {attempt+1}/3: {e}")
            time.sleep(4 ** attempt)
    return []


def fetch_all(sess, doctype, filters=None, page_size=500):
    total = get_count(sess, doctype, filters)
    if total == 0:
        print(f"  {doctype}: 0 records")
        return []
    print(f"  {doctype}: {total:,} records")
    rows = []
    for offset in range(0, total, page_size):
        batch = fetch_page(sess, doctype, offset, page_size, filters)
        if batch is _PERMISSION_DENIED:
            return []
        rows.extend(batch)
        time.sleep(0.3)
    print(f"  Done — {len(rows):,} rows")
    return rows


def hash_pii(df):
    def _h(v):
        if pd.isna(v) or str(v).strip() == "":
            return v
        return hashlib.sha256(str(v).encode()).hexdigest()
    for col in _DEFAULT_PII_COLS:
        if col in df.columns:
            df[col] = df[col].apply(_h)
    return df


def table_name(doctype):
    return doctype.lower().replace(" ", "_")


def main():
    if not (EBUZIMA_USER and EBUZIMA_PASSWORD) and not (API_KEY and API_SECRET):
        print("ERROR: Set EBUZIMA_USER and EBUZIMA_PASSWORD (or API_KEY/SECRET) in .env")
        return

    export_dir = os.path.join(os.path.dirname(__file__), "data", "exports")
    os.makedirs(export_dir, exist_ok=True)

    sess    = _session()
    results = []
    t_start = time.time()

    for dt in TARGET_DOCTYPES:
        tbl = table_name(dt)
        print(f"\n{'─'*55}")
        print(f"  DocType : {dt}")
        t0 = time.time()
        try:
            rows = fetch_all(sess, dt)
        except Exception as e:
            print(f"  [error] {e}")
            results.append({"doctype": dt, "table": tbl, "fetched": 0,
                            "stored": 0, "destination": "error", "error": str(e)})
            continue

        if not rows:
            results.append({"doctype": dt, "table": tbl, "fetched": 0,
                            "stored": 0, "destination": "skipped"})
            continue

        df = pd.DataFrame(rows)
        df = hash_pii(df)

        out_path = os.path.join(export_dir, f"{tbl}.csv")
        df.to_csv(out_path, index=False)
        elapsed = time.time() - t0
        print(f"  Saved   : {len(df):,} rows → {out_path}  ({elapsed:.1f}s)")
        results.append({"doctype": dt, "table": tbl, "fetched": len(df),
                        "stored": len(df), "destination": "csv"})

    # ── Manifest ─────────────────────────────────────────────────────────────
    manifest_df = pd.DataFrame(results)
    manifest_df["run_at"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    manifest_path = os.path.join(export_dir, "manifest.csv")
    manifest_df.to_csv(manifest_path, index=False)

    # ── Summary ───────────────────────────────────────────────────────────────
    total_time = time.time() - t_start
    print(f"\n{'='*55}")
    print("INGESTION SUMMARY  (CSV fallback — sqlalchemy not available)")
    print(f"{'='*55}")
    print(manifest_df[["doctype","fetched","stored","destination"]].to_string(index=False))
    total_fetched = sum(r["fetched"] for r in results)
    print(f"\nTotal rows fetched : {total_fetched:,}")
    print(f"Manifest saved     : {manifest_path}")
    print(f"Duration           : {total_time:.1f}s")


if __name__ == "__main__":
    main()
