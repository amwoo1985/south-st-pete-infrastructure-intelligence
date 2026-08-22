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

from datetime import date, datetime, timezone

import psycopg
import pytest

from app.db.connection import get_connection
from app.granicus.schema import GRANICUS_JOB_COLUMNS, apply_granicus_schema


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


def insert_synthetic_job(
    conn: psycopg.Connection,
    mp3_url: str,
    *,
    meeting_title: str = "TEST SYNTHETIC — do not treat as a real meeting",
    source_url: str = "https://stpete.granicus.com/MediaPlayer.php?view_id=1&clip_id=999",
    published_date: date = date(2026, 1, 1),
    retrieval_timestamp: datetime | None = None,
    status: str = "pending",
    claimed_at: datetime | None = None,
    transcript_text: str | None = None,
    failure_reason: str | None = None,
) -> None:
    """Test-only helper: inserts a row directly (bypassing
    register.py's register_meeting(), which always inserts
    status='pending') so worker tests can set up synthetic 'pending' and
    'claimed'-with-arbitrary-claimed_at rows. Every field defaults to an
    obviously-synthetic value — `meeting_title` in particular is labeled
    so a row from this helper is never mistaken for a real registered
    meeting if a test forgets to clean up (tests/granicus/conftest.py's
    db_conn fixture rolls back regardless, but the label is a second,
    unambiguous signal for a human skimming the DB mid-debug).
    """
    effective_retrieval_timestamp = (
        retrieval_timestamp if retrieval_timestamp is not None else datetime.now(timezone.utc)
    )
    columns_sql = ", ".join(GRANICUS_JOB_COLUMNS)
    placeholders = ", ".join(["%s"] * len(GRANICUS_JOB_COLUMNS))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO granicus_transcription_jobs ({columns_sql}) VALUES ({placeholders})",
            (
                mp3_url,
                meeting_title,
                source_url,
                published_date,
                effective_retrieval_timestamp,
                status,
                claimed_at,
                transcript_text,
                failure_reason,
            ),
        )
