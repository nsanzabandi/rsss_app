# RSSS — GitHub + Server Deployment Guide

The app is served at **`http(s)://SERVER_IP/rsss_app/`** alongside other apps on the same server.  
Deployments use **GitHub as the source of truth**: push locally → pull on the server.

---

## Files in this `deploy/` directory

| File | Purpose |
|------|---------|
| `setup_server.sh` | Run **once** on the server — installs everything, clones from GitHub |
| `deploy.sh` | Run from **local machine** — push to GitHub + pull on server |
| `rsss.service` | systemd unit file (auto-installed by `setup_server.sh`) |
| `rsss_nginx.conf` | Nginx location config (auto-installed by `setup_server.sh`) |
| `env.production.example` | Template for `/opt/rsss_app/.env` on the server |

---

## Step-by-Step

### Step 1 — Push to GitHub (first time)

```bash
# From project root on your local machine:
cd /path/to/rsss_app

git init
git add -A
git commit -m "initial commit"
git branch -M main

# Create a repo on GitHub (github.com → New repository → rsss_app)
git remote add origin https://github.com/YOUR_ORG/rsss_app.git
git push -u origin main
```

> [!IMPORTANT]
> The `.gitignore` already excludes `.env`, `*.db`, `.venv/`, `logs/`, and generated reports.  
> **Never push `.env` to GitHub** — it contains secrets.

---

### Step 2 — Bootstrap the server (run ONCE)

Edit the config block at the top of `deploy/setup_server.sh`:

```bash
GITHUB_REPO="https://github.com/YOUR_ORG/rsss_app.git"
GIT_BRANCH="main"
NGINX_CONF_MODE="add-to-existing"   # ← use this since other apps are already running
```

> [!IMPORTANT]
> Because other apps are already running on your server, set `NGINX_CONF_MODE="add-to-existing"`.  
> The script will print the exact Nginx location blocks to add to your existing `server {}` block — it will **not** touch your existing Nginx config automatically.

Upload and run the script on the server:

```bash
# From your local machine:
scp deploy/setup_server.sh ubuntu@SERVER_IP:/tmp/
ssh ubuntu@SERVER_IP "sudo bash /tmp/setup_server.sh"
```

---

### Step 3 — Add RSSS to Nginx (existing server)

After `setup_server.sh` runs, it prints the location blocks. Add them to your server's Nginx config.

First, add the rate-limit zone inside the `http {}` block in `/etc/nginx/nginx.conf`:

```nginx
http {
    # ... existing config ...
    limit_req_zone $binary_remote_addr zone=rsss_login:10m rate=5r/m;
}
```

Then add these location blocks inside your existing `server {}` block:

```nginx
server {
    # ... your existing locations for other apps ...

    # RSSS static assets
    location /rsss_app/assets/ {
        alias /opt/rsss_app/assets/;
        expires 7d;
        add_header Cache-Control "public, immutable";
    }

    # RSSS login (rate-limited)
    location = /rsss_app/do-login {
        limit_req zone=rsss_login burst=3 nodelay;
        proxy_pass http://127.0.0.1:8050;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # RSSS main app
    location /rsss_app/ {
        proxy_pass         http://127.0.0.1:8050;
        proxy_http_version 1.1;
        proxy_set_header   Connection        "";
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_buffering    off;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }
}
```

Then test and reload:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

---

### Step 4 — Create production `.env` on the server

```bash
sudo cp /opt/rsss_app/deploy/env.production.example /opt/rsss_app/.env
sudo nano /opt/rsss_app/.env          # fill in all REPLACE_WITH_* values
sudo chown rsss:rsss /opt/rsss_app/.env
sudo chmod 600 /opt/rsss_app/.env
```

Key values to set:

| Variable | Value |
|----------|-------|
| `SECRET_KEY` | Run `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `DEBUG` | `false` |
| `APP_BASE_URL` | `http://SERVER_IP/rsss_app` |
| `DATA_BACKEND` | `local` (default) |
| `LOCAL_DB_PASSWORD` | Password you set in Step 2 |
| `EMAIL_USER` / `EMAIL_PASSWORD` | Your Gmail + App Password |
| `ETRACKER_USER/PASSWORD` | Your eTracker credentials |

---

### Step 5 — First data load (ETL sync)

```bash
sudo -u rsss /opt/rsss_app/.venv/bin/python -m sync.etl
```

This pulls all data from eTracker into the local PostgreSQL database. May take several minutes.

---

### Step 6 — Start the app

```bash
sudo systemctl start rsss
sudo systemctl status rsss
```

Test it:
```bash
curl -I http://localhost/rsss_app/
# Should return HTTP/1.1 200 OK
```

Open in browser: **`http://SERVER_IP/rsss_app/`**

---

## Deploying Updates (day-to-day)

Edit `deploy/deploy.sh` — set `SERVER_HOST` and `SERVER_USER`, then:

```bash
bash deploy/deploy.sh
```

This will:
1. Commit any uncommitted local changes
2. Push to GitHub
3. SSH into server → `git pull` → `pip install` → `systemctl restart rsss`

---

## Useful Server Commands

```bash
# Service
sudo systemctl status rsss
sudo systemctl restart rsss
sudo journalctl -u rsss -f          # live service logs

# App logs
sudo tail -f /opt/rsss_app/logs/error.log
sudo tail -f /opt/rsss_app/logs/access.log

# Nginx
sudo tail -f /var/log/nginx/rsss_error.log
sudo nginx -t && sudo systemctl reload nginx

# DB connectivity check
sudo -u rsss bash -c '
cd /opt/rsss_app
.venv/bin/python -c "from config.backend import get_conn; c=get_conn(); print(\"DB OK\"); c.close()"
'

# Manual ETL
sudo -u rsss /opt/rsss_app/.venv/bin/python -m sync.etl

# Test email (must be logged in — use browser)
curl http://localhost/rsss_app/test-email
```

---

## Architecture

```
Internet
    │
    ▼
 Nginx (port 80/443)        ← existing server, other apps on same server
    │
    ├─ /other-app/  ──────► other app (untouched)
    │
    └─ /rsss_app/  ───────► Gunicorn (127.0.0.1:8050)
                                │  1 worker, gevent, 4 threads
                                │  Background: auto-sync + auto-report threads
                                │
                                ▼
                         Plotly Dash / Flask
                                │
                                ▼
                         PostgreSQL (localhost:5432)
                         immunization_db
                              ↑
                         sync/etl.py ← eTracker DHIS2 API
```

---

## Security Checklist

- [ ] `SECRET_KEY` is random 32+ hex chars (not the default)
- [ ] `DEBUG=false` in `.env`
- [ ] `.env` is `chmod 600`, owned by `rsss`
- [ ] `config/users.db` is `chmod 600`, owned by `rsss`
- [ ] Port `8050` is NOT exposed publicly (gunicorn binds `127.0.0.1` only)
- [ ] Firewall: only SSH + 80 + 443 open (`sudo ufw status`)
- [ ] GitHub repo is private (contains app code but not secrets)
