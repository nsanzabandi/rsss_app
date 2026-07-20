"""
config/db_local.py — Local immunization_db connection config.

This database is populated by sync/etl.py pulling from the eTracker API
(tracker.moh.gov.rw). It stores the immunization_vaccination table.

No SSH tunnel required — runs on localhost:5432.
"""
from __future__ import annotations
import os


class LocalDBConfig:
    HOST     = os.environ.get("LOCAL_DB_HOST",     "localhost")
    PORT     = int(os.environ.get("LOCAL_DB_PORT", "5432"))
    NAME     = os.environ.get("LOCAL_DB_NAME",     "immunization_db")
    USER     = os.environ.get("LOCAL_DB_USER",     "postgres")
    PASSWORD = os.environ.get("LOCAL_DB_PASSWORD", "omop")

    def get_connection_kwargs(self) -> dict:
        return {
            "host":     self.HOST,
            "port":     self.PORT,
            "dbname":   self.NAME,
            "user":     self.USER,
            "password": self.PASSWORD,
            "connect_timeout": 5,   # fail fast — don't block the Dash worker
        }

    def get_dsn(self) -> str:
        return (
            f"host={self.HOST} port={self.PORT} dbname={self.NAME} "
            f"user={self.USER} password={self.PASSWORD}"
        )


local_db = LocalDBConfig()


def get_local_conn():
    """Return a new psycopg2 connection to the local immunization_db."""
    import psycopg2
    return psycopg2.connect(**local_db.get_connection_kwargs())


def test_local_conn() -> bool:
    """Return True if the local DB is reachable."""
    try:
        conn = get_local_conn()
        conn.close()
        return True
    except Exception:
        return False
