# RSSS — Rwanda Stunting Surveillance System
# Production image: Gunicorn serving app:server (see deploy/rsss.service for the
# equivalent systemd flags this mirrors).

FROM python:3.11-slim

WORKDIR /app

# psycopg2-binary ships prebuilt wheels, so no libpq-dev/gcc needed at runtime.
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -r -m -s /usr/sbin/nologin rsss \
    && chown -R rsss:rsss /app
USER rsss

EXPOSE 8050

# gthread (real threads), not gevent: under gevent, background work (post-sync
# cache refresh, report jobs, risk classifier) is CPU-bound pandas that never
# yields, so it froze every request until it finished. Still 1 worker — jobs,
# caches and the schedulers live in-process.
CMD ["gunicorn", "app:server", \
     "--workers", "1", \
     "--worker-class", "gthread", \
     "--threads", "8", \
     "--timeout", "120", \
     "--bind", "0.0.0.0:8050", \
     "--log-level", "info", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
