"""
config/app_config.py — Application-level constants.
"""
import os

# Public base URL used to build the view-only dashboard links in emails —
# including the /rsss_app prefix, e.g. http://197.243.95.46/rsss_app
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8050/rsss_app").rstrip("/")

# Whether report emails include a "view your dashboard" link: the national
# overview links to the national dashboard, each hospital/facility report to
# its own catchment. Links are view-only, dashboards only (no child lists)
# and expire after two months (auth.VIEW_LINK_MAX_AGE).
INCLUDE_DASHBOARD_LINK_IN_EMAILS = os.environ.get(
    "INCLUDE_DASHBOARD_LINK_IN_EMAILS", "true").strip().lower() == "true"

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

# ── Account requests ───────────────────────────────────────────────────────────
# Anyone can request an account at /register; a ministry admin approves it in
# Settings → Users & roles. This person is shown as the contact for help with
# a request. ACCOUNT_APPROVER_EMAILS (comma-separated, .env) are emailed when
# a new request arrives — leave empty to skip the notification.
ACCOUNT_HELP_NAME  = "Daniel Nsanzabandi"
ACCOUNT_HELP_PHONE = "0788 235 332"
ACCOUNT_APPROVER_EMAILS = [e.strip() for e in
                           os.environ.get("ACCOUNT_APPROVER_EMAILS", "").split(",") if e.strip()]

# ── Support ────────────────────────────────────────────────────────────────────
SUPPORT_PHONE   = "+250788489801"
SUPPORT_CONTACT = "Justin Ntaganda"
SUPPORT_EMAIL   = "justin.ntaganda@rbc.gov.rw"
ORG_NAME        = "Rwanda Biomedical Center"
ORG_DIVISION    = "Maternal, Child and Community Health Division"

