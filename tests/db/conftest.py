"""Fixtures for tests that exercise app/db and app/embeddings/pipeline
against the REAL local docker-compose Postgres (DECISIONS #68-70), not a
mock.

Test-DB strategy (DECISIONS #71): transactional-rollback, not a mocked
DB or a separate throwaway test database. This repo has no precedent for
mocking Postgres, and this pipeline's whole point is real SQL (upsert-by-
chunk_id, a real vector column) — a mock would just re-assert "the mock
does what the mock does." Instead, each test gets its own connection,
never commits (pipeline calls are made with `commit=False`), and the
fixture rolls back at teardown, so no test leaves rows behind in the
shared dev DB. Schema creation (CREATE TABLE/INDEX IF NOT EXISTS) is
applied once per test session, via its own short-lived committed
connection, since DDL needs to actually exist for later tests' queries to
plan against.

Every test in this file requires the docker-compose `db` service to be
up (`docker-compose up -d`) — these are not run as part of a fully
offline `pytest tests/`, mirroring how the crawler suite's `responses`-
mocked tests differ in kind from this real-DB suite.
"""

from __future__ import annotations

import pytest

from app.db.connection import get_connection
from app.db.schema import apply_schema


@pytest.fixture(scope="session", autouse=True)
def _schema_ready():
    conn = get_connection()
    try:
        apply_schema(conn)
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
