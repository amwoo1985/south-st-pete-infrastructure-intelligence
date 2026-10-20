"""Granicus meeting-transcription jobs table schema (DECISIONS #79-81).

Supersedes DECISIONS #12's automated-discovery mechanism only: meetings
are registered by a human-supplied direct MP3 URL (app.granicus.register),
not resolved by an automated RSS/MediaPlayer.php crawl of the
robots.txt-blocked stpete.granicus.com host (DECISIONS #77). #12's async-
worker pattern, hosted transcription API, and 12-month backfill bound are
all still binding and shape this table.

This module owns the ``granicus_transcription_jobs`` table's DDL only. It
does not register meetings (app/granicus/register.py) and does not
claim/process jobs (the Day 5+ polling worker, not built yet).

Table shape mirrors app/db/schema.py's ``chunks`` table conventions
(DECISIONS #71): one explicit COLUMNS constant reused by every INSERT/
SELECT (.claude/rules/data.md bars ``SELECT *``), nullable-over-sentinel
for anything not known at registration time, and the same Attribution
field names (source_url, published_date, retrieval_timestamp) used
everywhere else in this codebase rather than inventing new ones here.
"""

from __future__ import annotations

import psycopg

# Explicit column list, reused by every INSERT/SELECT this module writes
# — .claude/rules/data.md bars `SELECT *`.
#
# Column <-> concept mapping:
#   mp3_url              <- the direct archive-video.granicus.com URL a
#                           human resolved and supplied; the natural
#                           primary key (see DECISIONS #81 for why this is
#                           a natural key, not a hashed synthetic ID like
#                           chunks.chunk_id).
#   meeting_title         <- human-supplied
#   source_url            <- the MediaPlayer.php URL, attribution only,
#                           never fetched (Attribution.source_url)
#   published_date         <- the meeting date (Attribution.published_date,
#                           but NOT NULL here — always known at
#                           registration, unlike a scraped page's parse)
#   retrieval_timestamp    <- when this system recorded the registration
#                           (Attribution.retrieval_timestamp)
#   status                 <- 'pending' | 'claimed' | 'completed' | 'failed'
#   claimed_at              <- NULL until a Day 5+ worker claims the row;
#                           supports stale-row recovery
#   transcript_text         <- NULL until transcription completes
#   failure_reason          <- NULL unless status='failed'
GRANICUS_JOB_COLUMNS: tuple[str, ...] = (
    "mp3_url",
    "meeting_title",
    "source_url",
    "published_date",
    "retrieval_timestamp",
    "status",
    "claimed_at",
    "transcript_text",
    "failure_reason",
)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS granicus_transcription_jobs (
    mp3_url TEXT PRIMARY KEY,
    meeting_title TEXT NOT NULL,
    source_url TEXT NOT NULL,
    published_date DATE NOT NULL,
    retrieval_timestamp TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'completed', 'failed')),
    claimed_at TIMESTAMPTZ,
    transcript_text TEXT,
    failure_reason TEXT
);
"""

# Supports the Day 5+ worker's future claim query
# (WHERE status = 'pending' ... FOR UPDATE SKIP LOCKED) and its stale-row
# recovery query (WHERE status = 'claimed' AND claimed_at < cutoff) —
# added now, alongside the table, rather than as an afterthought once the
# worker exists. Same "add the index the next phase's known query
# pattern will need" reasoning as chunks' HNSW index (DECISIONS #71).
_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS granicus_jobs_status_idx
    ON granicus_transcription_jobs (status);
"""


def apply_granicus_schema(conn: psycopg.Connection) -> None:
    """Idempotent: safe to call on every process start (CREATE TABLE/INDEX
    IF NOT EXISTS). Commits its own transaction — call this once, before
    any registration writes, not per-registration."""
    with conn.cursor() as cur:
        cur.execute(_CREATE_TABLE_SQL)
        cur.execute(_CREATE_INDEX_SQL)
    conn.commit()
