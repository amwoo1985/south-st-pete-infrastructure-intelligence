"""Shared pytest configuration. Per-area fixtures live in each test
subpackage's own conftest.py (tests/api, tests/db, tests/rag).

Every test that touches Postgres runs against a schema brought current by
the real migration runner (app/db/migrate.py) once per session — the same
path a deploy uses, not a test-only shortcut.
"""

from __future__ import annotations

import psycopg
import pytest


@pytest.fixture(scope="session", autouse=True)
def _migrated_schema():
    from app.db.connection import get_migration_connection
    from app.db.migrate import up

    try:
        conn = get_migration_connection()
    except psycopg.OperationalError:
        # DB not running (pure unit-test run): DB-backed tests fail on their
        # own connection attempts. A missing .env value is NOT caught here;
        # it fails loud at session start.
        yield
        return
    try:
        up(conn)
    finally:
        conn.close()
    yield
