"""Fixtures for tests that exercise app/granicus against the REAL local
docker-compose Postgres (DECISIONS #68-70), not a mock — same
transactional-rollback strategy as tests/db/conftest.py (DECISIONS #71),
mirrored here rather than imported, matching this repo's existing
one-conftest-per-test-subdirectory convention.

Every test in this file requires the docker-compose `db` service to be
up (`docker-compose up -d`) — not run as part of a fully offline
`pytest tests/`.
"""

from __future__ import annotations

import pytest

from app.db.connection import get_connection
from app.granicus.schema import apply_granicus_schema


@pytest.fixture(scope="session", autouse=True)
def _granicus_schema_ready():
    conn = get_connection()
    try:
        apply_granicus_schema(conn)
    finally:
        conn.close()


@pytest.fixture
def db_conn():
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
