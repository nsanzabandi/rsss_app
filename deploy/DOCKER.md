# RSSS — Docker Deployment Guide

This is the deployment path for a **fresh, dedicated** Ubuntu server, using Docker Compose so
day-to-day updates are just `bash deploy/docker-deploy.sh`. `deploy/README.md` (systemd + a bare
venv) stays as the reference for the older shared-server setup — you don't need it here.

Target server for this guide: `ssh -p 3126 danny@197.243.95.46`.

Three containers, one `docker-compose.yml`:

```
nginx (host port 80) → app (gunicorn, port 8050) → db (postgres:16, immunization_db + ebuzima_db)
```

The eBuzima module's ClickHouse mirror (`197.243.95.44`) and the eTracker/DHIS2 API are external
services — nothing to install for those, just credentials in `.env`.

---

## Step 1 — One-time server bootstrap

SSH in:
```bash
ssh -p 3126 danny@197.243.95.46
```

Install Docker Engine + Compose plugin:
```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker danny
newgrp docker   # or log out/in so group membership takes effect
docker compose version   # sanity check
```

Clone the repo:
```bash
sudo mkdir -p /opt/rsss_app
sudo chown danny:danny /opt/rsss_app
git clone git@github.com:nsanzabandi/rsss_app.git /opt/rsss_app
# (set up a deploy key or personal access token first if this is a private repo
#  and you haven't pushed your SSH key to GitHub yet)
cd /opt/rsss_app
```

Create the production `.env`:
```bash
cp deploy/env.production.example .env
nano .env
```

Fill in, at minimum:

| Variable | Value |
|---|---|
| `SECRET_KEY` | `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `DEBUG` | `false` |
| `APP_BASE_URL` | `http://197.243.95.46/rsss_app` |
| `LOCAL_DB_HOST` | `db`  ← Docker service name, **not** `localhost` |
| `LOCAL_DB_USER` / `LOCAL_DB_PASSWORD` / `LOCAL_DB_NAME` | pick real values — these seed the Postgres container on first boot |
| `EMAIL_USER` / `EMAIL_PASSWORD` | Gmail + App Password |
| `ETRACKER_USER` / `ETRACKER_PASSWORD` | eTracker (DHIS2) credentials |
| `EBUZIMA_DB_HOST` | `db` |
| `EBUZIMA_DB_USER` / `EBUZIMA_DB_PASSWORD` / `EBUZIMA_DB_NAME` | pick real values — a **separate** DB from `LOCAL_DB_*`, seeded by `deploy/postgres-init/01-ebuzima-db.sh` on first boot |
| `EBUZIMA_CLICKHOUSE_PASSWORD` | password for the external ClickHouse mirror at `197.243.95.44` |
| `EBUZIMA_API_KEY` / `EBUZIMA_API_SECRET` | only if the Frappe REST fallback is used |

> `LOCAL_DB_HOST`/`EBUZIMA_DB_HOST` **must** be `db` (the compose service name) — the app container
> reaches Postgres over the internal Docker network, not `localhost`.

Build and start everything:
```bash
docker compose up -d --build
docker compose ps      # all three should show "running"/"healthy"
```

---

## Step 2 — First-run data load

Pull the initial eTracker data into `immunization_db` (can take a few minutes):
```bash
docker compose exec app python -m sync.etl
```

Create your login:
```bash
docker compose exec app python tools/reset_admin.py <username> <password>
# or just: docker compose exec app python tools/reset_admin.py   (admin / ChangeMe@2026)
```

Build the child-level stunting cache once by hand so the dashboard has data immediately:
```bash
docker compose exec app python -m tools.build_child_cache
```

If eBuzima credentials are filled in, open the eBuzima page in the browser once logged in and use
its manual "Sync" button to pull the first batch from ClickHouse/Frappe.

---

## Step 3 — Verify

```bash
curl -I http://197.243.95.46/rsss_app/          # expect 200 OK
docker compose logs app --tail=100              # should be clean, no tracebacks
```

Open `http://197.243.95.46/rsss_app/` in a browser and log in.

---

## Step 4 — Schedule the recurring cache build

The dashboard's WHO-stunting computation is CPU-heavy; rebuild it on a schedule instead of relying
on the first visitor after a restart. On the **host** (not inside the container):

```bash
crontab -e
```

Add:
```
*/20 * * * * cd /opt/rsss_app && docker compose exec -T app python -m tools.build_child_cache >> /opt/rsss_app/logs/child_cache.log 2>&1
```

---

## Step 5 — Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw enable
sudo ufw status
```

Only `nginx` publishes a host port (80) — `app` and `db` are reachable only inside the Docker
network, so nothing else needs opening.

---

## Day-to-day updates

From your local machine, project root:
```bash
bash deploy/docker-deploy.sh
```

This commits + pushes local changes, then on the server: `git pull` → rebuild **only** the `app`
image → restart **only** the `app` container. `db` and `nginx` are left running, so Postgres data
and active connections aren't disturbed by a code deploy.

Useful commands:
```bash
docker compose ps
docker compose logs -f app
docker compose restart app
docker compose exec db psql -U <LOCAL_DB_USER> -d immunization_db -c '\dt'
docker compose exec app python -m sync.etl        # manual re-sync
```

---

## HTTPS (later)

Deferred until a domain is pointed at this server. Then either:
- add a `certbot` sidecar container sharing a webroot volume with `nginx`, or
- put a host-level reverse proxy (with its own certbot) in front of the `nginx` container's
  port 80/443.

---

## Security checklist

- [ ] `SECRET_KEY` is a random 32+ hex string (not the example default)
- [ ] `DEBUG=false` in `.env`
- [ ] `.env` is not committed to git (already covered by `.gitignore`)
- [ ] `LOCAL_DB_PASSWORD` / `EBUZIMA_DB_PASSWORD` are strong, unique passwords
- [ ] Only port 80 (and SSH) is open in `ufw`
- [ ] GitHub repo access uses a deploy key or PAT scoped to this repo
