#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# deploy.sh — Deploy RSSS from GitHub to the server
#
# Usage (from your local machine, project root):
#   bash deploy/deploy.sh
#
# What it does:
#   1. Pushes your local commits to GitHub
#   2. SSHes into the server and runs: git pull → pip install → service restart
#
# Prerequisites:
#   • Git repository initialized locally (git init + git remote add origin …)
#   • SSH key access to SERVER_USER@SERVER_HOST
#   • setup_server.sh already ran on the server (cloned the repo)
# ══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Config (edit these) ───────────────────────────────────────────────────────
SERVER_HOST="197.243.95.43"   # ← e.g. 192.168.1.100
SERVER_USER="ubuntu"                       # ← SSH login user (with sudo)
APP_DIR="/opt/rsss_app"
SERVICE_USER="rsss"
SERVICE_NAME="rsss"
GIT_BRANCH="main"                          # ← branch to deploy

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[deploy]${NC} $*"; }
warn()  { echo -e "${YELLOW}[deploy]${NC} $*"; }
error() { echo -e "${RED}[deploy]${NC} $*"; exit 1; }

# ══════════════════════════════════════════════════════════════════════════════
info "Step 1 — Pushing local commits to GitHub (branch: $GIT_BRANCH)…"
# ══════════════════════════════════════════════════════════════════════════════
if ! git diff --quiet || ! git diff --cached --quiet; then
    warn "You have uncommitted changes. Committing them automatically…"
    git add -A
    git commit -m "deploy: $(date '+%Y-%m-%d %H:%M')"
fi
git push origin "$GIT_BRANCH"
info "  GitHub push complete."

# ══════════════════════════════════════════════════════════════════════════════
info "Step 2 — Pulling on server and restarting service…"
# ══════════════════════════════════════════════════════════════════════════════
ssh "$SERVER_USER@$SERVER_HOST" bash << EOF
set -e

echo "  → git pull…"
sudo -u $SERVICE_USER git -C $APP_DIR pull origin $GIT_BRANCH

echo "  → pip install (new/updated packages)…"
sudo -u $SERVICE_USER $APP_DIR/.venv/bin/pip install --quiet -r $APP_DIR/requirements.txt

echo "  → restarting $SERVICE_NAME…"
sudo systemctl restart $SERVICE_NAME
sleep 3
sudo systemctl status $SERVICE_NAME --no-pager -l
EOF

# ══════════════════════════════════════════════════════════════════════════════
info "✅  Deploy complete!"
echo ""
echo "  App URL:    http://$SERVER_HOST/rsss_app/"
echo "  Logs:       sudo tail -f $APP_DIR/logs/error.log"
echo "  Service:    sudo systemctl status $SERVICE_NAME"
echo ""
