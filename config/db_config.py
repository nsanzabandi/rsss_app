"""
config/db_config.py — Production PostgreSQL connection parameters.

All values come from environment variables (set in .env). The defaults are only
a local-dev fallback; put real credentials in .env, never in code.
The database is accessed over an SSH tunnel (default port 15432).
"""
import os


class DatabaseConfig:
    DB_HOST     = os.environ.get("PROD_DB_HOST",     "127.0.0.1")
    DB_PORT     = int(os.environ.get("PROD_DB_PORT", "15432"))
    DB_NAME     = os.environ.get("PROD_DB_NAME",     "metabase")
    DB_USER     = os.environ.get("PROD_DB_USER",     "metabase")
    DB_PASSWORD = os.environ.get("PROD_DB_PASSWORD", "")

    def get_connection_kwargs(self) -> dict:
        return dict(
            host=self.DB_HOST,
            port=self.DB_PORT,
            database=self.DB_NAME,
            user=self.DB_USER,
            password=self.DB_PASSWORD,
        )


db_config = DatabaseConfig()
