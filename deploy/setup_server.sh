#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# setup_server.sh — One-shot server bootstrap for RSSS
#
# Run ONCE on a fresh Ubuntu 22.04 LTS server as root (sudo su or sudo bash).
# Usage:  sudo bash setup_server.sh
#
# What this does:
#   1. Install system packages (Python, Nginx, PostgreSQL, Git, Certbot)
#   2. Create the 'rsss' service account
#   3. Clone the app from GitHub → /opt/rsss_app
#   4. Create the Python virtual environment + install dependencies
#   5. Set up PostgreSQL user + database
#   6. Install the systemd service
#   7. Install the Nginx location block into the server's config
#
# After this script completes:
#   • Copy/create /opt/rsss_app/.env  (see env.production.example)
#   • sudo systemctl start rsss
#   • Run the initial ETL sync
# ══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ══════════════════════════════════════════════════════════════════════════════
# ── CONFIG — edit before running ─────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
GITHUB_REPO="https://github.com/YOUR_ORG/rsss_app.git"   # ← your GitHub URL
GIT_BRANCH="main"
APP_DIR="/opt/rsss_app"
SERVICE_USER="rsss"
DB_NAME="immunization_db"
DB_USER="rsss_db_user"
DB_PASS=""            # prompted below if empty
NGINX_CONF_MODE="standalone"   # 'standalone' or 'add-to-existing'
                               # standalone  → creates a new server {} block
                               # add-to-existing → only prints the location blocks to add manually
# ══════════════════════════════════════════════════════════════════════════════

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${GREEN}[setup]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[setup]${NC}  $*"; }
section() { echo -e "\n${CYAN}══ $* ${NC}"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Please run as root:  sudo bash $0"
[[ "$GITHUB_REPO" == *"YOUR_ORG"* ]] && error "Set GITHUB_REPO at the top of this script first."

if [[ -z "$DB_PASS" ]]; then
    read -rsp "Enter PostgreSQL password for '$DB_USER': " DB_PASS; echo
    [[ -z "$DB_PASS" ]] && error "DB password cannot be empty."
fi

# ══════════════════════════════════════════════════════════════════════════════
section "1/7  System packages"
# ══════════════════════════════════════════════════════════════════════════════
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv python3-dev \
    libpq-dev build-essential \
    postgresql postgresql-contrib \
    nginx \
    certbot python3-certbot-nginx \
    git curl
info "System packages installed."

# ══════════════════════════════════════════════════════════════════════════════
section "2/7  Service user '$SERVICE_USER'"
# ══════════════════════════════════════════════════════════════════════════════
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd -r -m -s /bin/bash "$SERVICE_USER"
    info "Created user: $SERVICE_USER"
else
    warn "User '$SERVICE_USER' already exists — skipping."
fi

# ══════════════════════════════════════════════════════════════════════════════
section "3/7  Clone from GitHub → $APP_DIR"
# ══════════════════════════════════════════════════════════════════════════════
if [ -d "$APP_DIR/.git" ]; then
    warn "$APP_DIR already contains a git repo — pulling latest instead of cloning."
    sudo -u "$SERVICE_USER" git -C "$APP_DIR" pull origin "$GIT_BRANCH"
else
    sudo -u "$SERVICE_USER" git clone --branch "$GIT_BRANCH" "$GITHUB_REPO" "$APP_DIR"
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
chmod 750 "$APP_DIR"
# Make sure log and report dirs exist and are writable
mkdir -p "$APP_DIR"/{logs,reports}
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"/{logs,reports}
chmod 770 "$APP_DIR"/{logs,reports}
info "Code cloned to $APP_DIR"

# ══════════════════════════════════════════════════════════════════════════════
section "4/7  Python virtual environment + dependencies"
# ══════════════════════════════════════════════════════════════════════════════
sudo -u "$SERVICE_USER" bash -c "
    python3 -m venv $APP_DIR/.venv
    $APP_DIR/.venv/bin/pip install --quiet --upgrade pip
    $APP_DIR/.venv/bin/pip install --quiet -r $APP_DIR/requirements.txt
"
info "Python venv ready at $APP_DIR/.venv"

# ══════════════════════════════════════════════════════════════════════════════
section "5/7  PostgreSQL: user + database"
# ══════════════════════════════════════════════════════════════════════════════
systemctl enable --now postgresql

sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';" 2>/dev/null \
    || warn "DB user '$DB_USER' may already exist."
sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;" 2>/dev/null \
    || warn "Database '$DB_NAME' may already exist."
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"
info "PostgreSQL: user=$DB_USER, database=$DB_NAME"

# ══════════════════════════════════════════════════════════════════════════════
section "6/7  systemd service"
# ══════════════════════════════════════════════════════════════════════════════
SVCFILE="$APP_DIR/deploy/rsss.service"
[[ ! -f "$SVCFILE" ]] && error "Service file not found: $SVCFILE"
cp "$SVCFILE" /etc/systemd/system/rsss.service
sed -i "s|/opt/rsss_app|$APP_DIR|g"       /etc/systemd/system/rsss.service
sed -i "s|User=rsss|User=$SERVICE_USER|g"  /etc/systemd/system/rsss.service
sed -i "s|Group=rsss|Group=$SERVICE_USER|g" /etc/systemd/system/rsss.service
systemctl daemon-reload
systemctl enable rsss
info "systemd service installed (not started yet — configure .env first)."

# ══════════════════════════════════════════════════════════════════════════════
section "7/7  Nginx configuration"
# ══════════════════════════════════════════════════════════════════════════════
systemctl enable --now nginx

if [[ "$NGINX_CONF_MODE" == "standalone" ]]; then
    # Install RSSS as the only/primary site
    cp "$APP_DIR/deploy/rsss_nginx.conf" /etc/nginx/sites-available/rsss
    ln -sf /etc/nginx/sites-available/rsss /etc/nginx/sites-enabled/rsss
    rm -f /etc/nginx/sites-enabled/default
    nginx -t || error "Nginx config test failed."
    systemctl reload nginx
    info "Nginx standalone config installed."
else
    echo ""
    warn "NGINX_CONF_MODE=add-to-existing: add the following to your existing server {} block:"
    echo ""
    echo '    location /rsss_app/assets/ {'
    echo "        alias $APP_DIR/assets/;"
    echo '        expires 7d;'
    echo '    }'
    echo '    location = /rsss_app/do-login {'
    echo '        limit_req zone=rsss_login burst=3 nodelay;'
    echo '        proxy_pass http://127.0.0.1:8050;'
    echo '        proxy_set_header Host $host;'
    echo '        proxy_set_header X-Real-IP $remote_addr;'
    echo '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;'
    echo '        proxy_set_header X-Forwarded-Proto $scheme;'
    echo '    }'
    echo '    location /rsss_app/ {'
    echo '        proxy_pass http://127.0.0.1:8050;'
    echo '        proxy_http_version 1.1;'
    echo '        proxy_set_header Connection "";'
    echo '        proxy_set_header Host $host;'
    echo '        proxy_set_header X-Real-IP $remote_addr;'
    echo '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;'
    echo '        proxy_set_header X-Forwarded-Proto $scheme;'
    echo '        proxy_buffering off;'
    echo '        proxy_read_timeout 120s;'
    echo '    }'
    echo ""
    warn "Also add this in the http {} block of /etc/nginx/nginx.conf:"
    echo "    limit_req_zone \$binary_remote_addr zone=rsss_login:10m rate=5r/m;"
    echo ""
fi

# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  RSSS bootstrap complete!                                 ║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Create the production .env:"
echo "       sudo cp $APP_DIR/deploy/env.production.example $APP_DIR/.env"
echo "       sudo nano $APP_DIR/.env          # fill in real values"
echo "       sudo chown $SERVICE_USER:$SERVICE_USER $APP_DIR/.env"
echo "       sudo chmod 600 $APP_DIR/.env"
echo ""
echo "  2. Protect config/users.db:"
echo "       sudo chown $SERVICE_USER:$SERVICE_USER $APP_DIR/config/users.db"
echo "       sudo chmod 600 $APP_DIR/config/users.db"
echo ""
echo "  3. Run the first ETL sync (populates immunization_db):"
echo "       sudo -u $SERVICE_USER $APP_DIR/.venv/bin/python -m sync.etl"
echo ""
echo "  4. Start the app:"
echo "       sudo systemctl start rsss"
echo "       sudo systemctl status rsss"
echo ""
echo "  5. Test:"
echo "       curl http://localhost/rsss_app/"
echo ""
echo "  6. (Optional) Enable HTTPS if you have a domain:"
echo "       sudo certbot --nginx -d your.domain.com"
echo ""
