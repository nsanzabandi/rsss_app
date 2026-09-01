"""eBuzima Nutrition Analytics — ingest + analytics engine for pages/ebuzima.py.

Ported from the standalone ebuzima_nutrition project. Uses its OWN PostgreSQL
and ClickHouse connections (EBUZIMA_* env vars) — entirely separate from
rsss_app's own immunization_db / eTracker pipeline.
"""
