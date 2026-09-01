"""
tools/build_child_cache.py — Build the heavy per-child WHO-stunting computation
and persist it to disk, decoupled from the web app process.

Why: computing WHO stunting classification across the full multi-visit history
(~1.2M+ children) is CPU-bound and takes roughly 2 minutes. Running it inside
the same process that serves the dashboard means:
  - any system load spike can make users wait many minutes for KPI numbers
    (the computation and user requests compete for the same CPU/GIL), and
  - every app restart starts from zero, so the dashboard is blank/slow for the
    first ~2 minutes after every restart.

Running this script on a schedule instead means the app only ever *reads* an
already-built result (data/cache/*.pkl) — near-instant, and unaffected by
whatever else is happening on the machine or how recently the app restarted.
data.py's warm_child_level()/get_child_df() etc. already check this disk
cache first and fall back to an in-process build only if it's missing/stale.

Usage — run once manually:
    python -m tools.build_child_cache

Usage — on a schedule (recommended, every 15-30 min):
    # crontab -e
    */20 * * * * cd /opt/rsss_app && .venv/bin/python -m tools.build_child_cache >> logs/child_cache.log 2>&1

    # or a systemd timer/service pair — see deploy/README.md
"""
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")


def main() -> int:
    import data

    t0 = time.time()
    print(f"[build_child_cache] starting…")

    meas = data.get_measurements_df()
    if meas is None or meas.empty:
        print("[build_child_cache] no measurements available — aborting")
        return 1

    child, monthly, schedule, visits = data._build_child_and_monthly(meas)
    data.save_child_cache_to_disk(child, monthly, schedule, visits)

    print(f"[build_child_cache] done in {time.time()-t0:.1f}s — "
          f"{len(child):,} children, {len(visits):,} visits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
