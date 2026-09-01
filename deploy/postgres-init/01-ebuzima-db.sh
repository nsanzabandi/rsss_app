#!/bin/sh
# Runs once, automatically, the first time the `db` container initializes its
# data directory (docker-entrypoint-initdb.d convention). The main
# POSTGRES_DB/USER/PASSWORD env vars already provisioned immunization_db —
# this creates the SEPARATE eBuzima role + database alongside it.
set -eu

if [ -z "${EBUZIMA_DB_NAME:-}" ] || [ -z "${EBUZIMA_DB_USER:-}" ] || [ -z "${EBUZIMA_DB_PASSWORD:-}" ]; then
    echo "[ebuzima-db-init] EBUZIMA_DB_NAME/USER/PASSWORD not set — skipping eBuzima database creation."
    exit 0
fi

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    SELECT 'CREATE ROLE "${EBUZIMA_DB_USER}" LOGIN PASSWORD ''${EBUZIMA_DB_PASSWORD}'''
    WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${EBUZIMA_DB_USER}')\gexec

    SELECT 'CREATE DATABASE "${EBUZIMA_DB_NAME}" OWNER "${EBUZIMA_DB_USER}"'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${EBUZIMA_DB_NAME}')\gexec

    GRANT ALL PRIVILEGES ON DATABASE "${EBUZIMA_DB_NAME}" TO "${EBUZIMA_DB_USER}";
SQL

echo "[ebuzima-db-init] ensured role '${EBUZIMA_DB_USER}' and database '${EBUZIMA_DB_NAME}'."
