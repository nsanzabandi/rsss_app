"""
config/app_config.py — Application-level constants.
"""
import os

# Public base URL used to build the view-only dashboard links in emails.
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8050").rstrip("/")

# Whether report emails include a "view your live dashboard" link. Disabled for
# now (link removed from outgoing emails) — set back to True to re-enable once
# the link is ready to share.
INCLUDE_DASHBOARD_LINK_IN_EMAILS = False

# Public (no login) view: national dashboard + at-risk dashboard, aggregates
# only — no child lists, downloads or operational pages. Signing in unlocks
# everything the user's role allows. Set PUBLIC_VIEW=false in .env to require
# login for everything.
PUBLIC_VIEW = os.environ.get("PUBLIC_VIEW", "true").strip().lower() == "true"

# ── Email ──────────────────────────────────────────────────────────────────────
# All emails in dry-run mode are redirected here instead of real recipients.
TEST_EMAIL = "nsanzabandidani@gmail.com"   # dry-run redirect target

# ── Reports ────────────────────────────────────────────────────────────────────
REPORTS_DIR = "reports"

# ── Stunting thresholds ────────────────────────────────────────────────────────
SENIOR_AGE_THRESHOLD_MONTHS = 15   # Children ≥15 months get the "senior" report

# ── Support ────────────────────────────────────────────────────────────────────
SUPPORT_PHONE   = "+250788489801"
SUPPORT_CONTACT = "Justin Ntaganda"
SUPPORT_EMAIL   = "justin.ntaganda@rbc.gov.rw"
ORG_NAME        = "Rwanda Biomedical Center"
ORG_DIVISION    = "Maternal, Child and Community Health Division"

