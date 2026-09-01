"""Run this once to see the real table names in ClickHouse.

    python -m ebuzima.check_clickhouse_tables

Use the output to fill in TABLE_MAP at the top of ingest.py — the defaults
there assume Frappe's `tabDocType` naming convention, which may not match
how your ClickHouse tables were actually created/replicated.
"""
from .clickhouse_source import list_tables

if __name__ == "__main__":
    tables = list_tables()
    print(f"Found {len(tables)} tables in ClickHouse:\n")
    for t in sorted(tables):
        print(f"  {t}")
