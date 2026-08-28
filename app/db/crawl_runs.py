"""``crawl_runs`` table DDL and insert helper — per-source-per-run health
tracking for the 13 chunk-producing source segments
`scripts/run_live_recrawl.py` drives (DECISIONS #146).

Why this exists: only Granicus (app/granicus/schema.py) had persistent
run/status tracking before this. The Tier-1 crawlers' outcomes
(`PipelineResult.inserted/skipped/failed`, app/embeddings/pipeline.py) were
only ever printed to stdout by `run_live_recrawl.py` — nothing survived
past the process exiting, so a silently-broken source in the unattended
weekly recrawl (DECISIONS #126/#134) had no trace outside CloudWatch logs.
This module owns the table that closes that gap. It does not decide *when*
a row gets written (`scripts/run_live_recrawl.py`'s job) and does not
build the read-side API endpoint (layer 2, api-layer's territory).

Mirrors app/api/schema.py / app/granicus/schema.py's pattern: a sibling
apply_*_schema() function, not folded into app/db/schema.py (which
docstring-scopes itself to `chunks` only, DECISIONS #71).

Primary key (DECISIONS #146): composite `(run_id, source_name)`, not a
surrogate autoincrement id. `run_id` (a UUID generated once per
`run_live_recrawl.py` invocation) already uniquely identifies one crawl
attempt, and each of the 13 source segments runs at most once per
invocation — so the composite key is a real natural key, consistent with
every other table in this codebase preferring a natural key over a
surrogate one (chunks.chunk_id, granicus_transcription_jobs.mp3_url,
document_uploads.file_hash).

Nullable-over-sentinel (.claude/rules/data.md): `inserted`/`skipped`/
`chunks_failed` are NULL only when the source's crawl step itself raised
before `embed_and_insert_chunks` ever ran, so there is no real
PipelineResult to report at all. NULL here means "never computed", not
"computed as zero" — a real zero (a source that crawled fine but produced
no new chunks) is a legitimate, distinct value these columns must still be
able to hold. Note `outcome='failed'` does NOT always imply these are
NULL: a source can crawl successfully, produce chunks, and still have
`outcome='failed'` if EVERY one of those chunks then failed to
embed/insert (scripts/run_live_recrawl.py's `run()` treats a 100%
embed/insert washout as a real failure, not a silent "success" with a
buried non-zero `chunks_failed` — crawler-review pre-commit finding,
DECISIONS #146) — that row carries the real, non-NULL `chunks_failed`
count plus an `error_message` explaining the washout.

Not carrying source_url/published_date/retrieval_timestamp
(.claude/rules/crawler.md's mandatory-attribution triad): that rule binds
per-ITEM provenance for content entering the corpus (a grant listing, a
meeting record) — each such item already carries its own Attribution via
`chunks.source_url`/`published_date`/`retrieval_timestamp`. A crawl_runs
row is operational telemetry about one source's crawl ATTEMPT, not a
content item, and a single source segment fetches many URLs (e.g.
stpete_grant_category_tiles hits 3 separate pages) — there is no one
stable URL to force into a single column here without it being
misleading. `started_at`/`finished_at` (both TIMESTAMPTZ NOT NULL, real
wall-clock captured immediately before/after this segment's crawl+embed
work) are this table's own timestamp pair, deliberately named
differently from Attribution's fields to not imply they mean the same
thing.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

import psycopg

logger = logging.getLogger("db.crawl_runs")

# Explicit column list, reused by the one INSERT this module writes —
# .claude/rules/data.md bars `SELECT *` / ad hoc column lists.
CRAWL_RUN_COLUMNS: tuple[str, ...] = (
    "run_id",
    "source_name",
    "started_at",
    "finished_at",
    "outcome",
    "inserted",
    "skipped",
    "chunks_failed",
    "error_message",
)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id UUID NOT NULL,
    source_name TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failed')),
    inserted INTEGER,
    skipped INTEGER,
    chunks_failed INTEGER,
    error_message TEXT,
    PRIMARY KEY (run_id, source_name)
);
"""

# Supports layer 2's known query shape: "what's the latest status per
# source" (ORDER BY started_at DESC LIMIT 1 per source_name) and "show me
# every failure" — added alongside the table, same reasoning as chunks'
# HNSW index and granicus_transcription_jobs' status index (DECISIONS #71,
# #79-81): add the index the next phase's known query pattern will need,
# not as an afterthought once that phase exists.
_CREATE_INDEXES_SQL = (
    """
    CREATE INDEX IF NOT EXISTS crawl_runs_source_started_idx
        ON crawl_runs (source_name, started_at DESC);
    """,
    """
    CREATE INDEX IF NOT EXISTS crawl_runs_outcome_idx
        ON crawl_runs (outcome);
    """,
)


def apply_crawl_runs_schema(conn: psycopg.Connection) -> None:
    """Idempotent: safe to call on every process start (CREATE TABLE/INDEX
    IF NOT EXISTS). Commits its own transaction — call this once, before
    any crawl_runs inserts, not per-insert."""
    with conn.cursor() as cur:
        cur.execute(_CREATE_TABLE_SQL)
        for index_sql in _CREATE_INDEXES_SQL:
            cur.execute(index_sql)
    conn.commit()


_INSERT_SQL = f"""
INSERT INTO crawl_runs ({", ".join(CRAWL_RUN_COLUMNS)})
VALUES ({", ".join(["%s"] * len(CRAWL_RUN_COLUMNS))})
"""


def record_crawl_run(
    conn: psycopg.Connection,
    *,
    run_id: str,
    source_name: str,
    started_at: datetime,
    finished_at: datetime,
    outcome: Literal["success", "failed"],
    inserted: int | None = None,
    skipped: int | None = None,
    chunks_failed: int | None = None,
    error_message: str | None = None,
) -> None:
    """Records one source segment's outcome for one crawl run. Always
    writes a row — a source that raised mid-crawl (outcome='failed') gets
    a real, visible row with `error_message` set, never a silently-missing
    one that would look like "nothing happened" for that source
    (.claude/rules/crawler.md's fail-loud rule, applied to the run-tracking
    table itself, not just the crawler that produced the failure).

    Best-effort, deliberately non-raising for ORDINARY write failures
    (data.md's cleanup-on-partial-failure spirit, applied in the other
    direction: this is the ledger write itself, not cleanup of an earlier
    one, but the same principle — a failure writing TELEMETRY about a
    crawl must never abort or mask the real crawl work for the sources
    still left to run in this invocation). On an ordinary failure to
    write (a bad value, a constraint violation), this logs at ERROR
    (visible in CloudWatch for the unattended weekly recrawl) and rolls
    back so the shared connection stays usable for the next source's
    work, but does not re-raise.

    Exception: `psycopg.OperationalError`/`InterfaceError` (a dropped
    connection, server gone away) ARE re-raised (crawler-review pre-commit
    finding, DECISIONS #146). Swallowing those here would be worse than
    the gap this table exists to close: the caller's own already-embedded
    chunks for this source were committed for real by
    `embed_and_insert_chunks` before this call, but if the connection then
    dies specifically during THIS insert, silently continuing on a dead
    connection would leave that source with ZERO rows in `crawl_runs` for
    this run_id — indistinguishable from "never attempted", not even a
    'failed' row. A systemic connection failure must abort loudly here
    too, same as it does in scripts/run_live_recrawl.py's `run()`.
    """
    values = (
        run_id,
        source_name,
        started_at,
        finished_at,
        outcome,
        inserted,
        skipped,
        chunks_failed,
        error_message,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(_INSERT_SQL, values)
        conn.commit()
    except (psycopg.OperationalError, psycopg.InterfaceError):
        raise
    except Exception as exc:
        logger.error(
            "failed to record crawl_runs row for run_id=%s source_name=%s "
            "(outcome=%s): %s",
            run_id,
            source_name,
            outcome,
            exc,
        )
        try:
            conn.rollback()
        except Exception as rollback_exc:
            logger.error(
                "rollback also failed after crawl_runs insert failure: %s",
                rollback_exc,
            )
