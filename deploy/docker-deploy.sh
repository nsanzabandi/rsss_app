#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# docker-deploy.sh — Deploy RSSS (Docker) from GitHub to the server
#
# Usage (from your local machine, project root):
#   bash deploy/docker-deploy.sh
#
# What it does:
#   1. Pushes your local commits to GitHub
#   2. SSHes into the server, git pulls, rebuilds + restarts ONLY the `app`
#      container (db/nginx keep running — Postgres data & connections untouched)
#
# Prerequisites:
#   • deploy/DOCKER.md's one-time server bootstrap already done
#     (Docker + Compose installed, repo cloned, .env created on the server)
#   • SSH key access to SERVER_USER@SERVER_HOST:SERVER_PORT
# ══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Config (edit these) ───────────────────────────────────────────────────────
SERVER_HOST="197.243.95.46"
SERVER_PORT="3126"
SERVER_USER="danny"
APP_DIR="/opt/rsss_app"
GIT_BRANCH="main"

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
info "Step 2 — Pulling on server, rebuilding + restarting the app container…"
# ══════════════════════════════════════════════════════════════════════════════
ssh -p "$SERVER_PORT" "$SERVER_USER@$SERVER_HOST" bash << EOF
set -e
cd $APP_DIR

echo "  → git pull…"
git pull origin $GIT_BRANCH

echo "  → docker compose build app…"
docker compose build app

echo "  → docker compose up -d app…"
docker compose up -d app

echo "  → pruning dangling images…"
docker image prune -f

sleep 3
docker compose ps
EOF

# ══════════════════════════════════════════════════════════════════════════════
info "✅  Deploy complete!"
echo ""
echo "  App URL:  http://$SERVER_HOST/rsss_app/"
echo "  Logs:     ssh -p $SERVER_PORT $SERVER_USER@$SERVER_HOST 'cd $APP_DIR && docker compose logs -f app'"
echo ""
