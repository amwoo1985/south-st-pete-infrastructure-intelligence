"""GET /health/crawlers — layer 2 of the crawler-health-visibility feature
(DECISIONS #146 built layer 1: app/db/crawl_runs.py's `crawl_runs` table).

Replaces the README's old "checking means looking at the job-status
tables directly" sentence: this endpoint surfaces, for each of the 13
Tier-1 source segments `scripts/run_live_recrawl.py` drives AND the
separate Granicus transcription pipeline, the latest known status (ok /
failed / never_run) plus enough detail (when it last ran, what broke) for
a non-engineer to tell something is wrong without querying Postgres
directly.

Read-only against two tables this module does not own or migrate:
`crawl_runs` (app/db/crawl_runs.py) and `granicus_transcription_jobs`
(app/granicus/schema.py) — both already created by their owning modules'
apply_*_schema() calls in app/api/main.py's lifespan / scripts/
run_live_recrawl.py's run(). This module issues no DDL.

TIER1_SOURCE_NAMES below is a deliberate, disclosed duplication of the 13
source_name values `scripts/run_live_recrawl.py`'s
`_build_source_segments()` returns (DECISIONS #146) — not imported from
there, because that script pulls in every Tier-1 crawler class
(app/crawlers/*) at import time for live network work this read-only API
process has no reason to load. Source list is closed (DECISIONS #11); if
a 14th source is ever added, it needs a new DECISIONS.md entry AND an
update to this tuple to be visible here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import get_db_conn
from app.api.schemas import CrawlerHealthResponse, CrawlerSourceStatus, GranicusHealthStatus

logger = logging.getLogger("api.crawler_health")

router = APIRouter()

# Mirrors scripts/run_live_recrawl.py's `_build_source_segments()` names —
# see module docstring for why this is a deliberate duplication, not an
# import.
TIER1_SOURCE_NAMES: tuple[str, ...] = (
    "legistar",
    "ami_table",
    "pinellas_cf",
    "arpa",
    "stpete_commitment",
    "spha_program_pages",
    "spha_news",
    "pinellas_hcd_programs",
    "pinellas_hcd_department",
    "stpete_grant_category_tiles",
    "stpete_south_stpete_summaries",
    "stpete_sunrise",
    "stpete_program_details",
)

# Explicit column list (.claude/rules/data.md bars SELECT *), reused by
# the one query below. Excludes run_id — this endpoint reports the latest
# status per source, not "which invocation" (that's DECISIONS #146's
# disclosed "all sources from one invocation" query shape, not needed
# here).
_SOURCE_STATUS_COLUMNS: tuple[str, ...] = (
    "source_name",
    "started_at",
    "finished_at",
    "outcome",
    "inserted",
    "skipped",
    "chunks_failed",
    "error_message",
)

# Uses crawl_runs' own (source_name, started_at DESC) index (app/db/
# crawl_runs.py) — exactly the "latest status per source" query shape
# that index/table's own docstring names as the layer-2 use case.
_LATEST_PER_SOURCE_SQL = f"""
SELECT DISTINCT ON (source_name) {", ".join(_SOURCE_STATUS_COLUMNS)}
FROM crawl_runs
ORDER BY source_name, started_at DESC
"""

_GRANICUS_STATUS_COUNTS_SQL = """
SELECT status, COUNT(*) FROM granicus_transcription_jobs GROUP BY status
"""

# NULLS LAST: a failed row with no claimed_at (failed before ever being
# claimed) must not silently outrank a more recent claimed-then-failed
# row just because Postgres's default DESC ordering puts NULLs first.
_GRANICUS_LATEST_FAILURE_SQL = """
SELECT mp3_url, meeting_title, failure_reason
FROM granicus_transcription_jobs
WHERE status = 'failed'
ORDER BY claimed_at DESC NULLS LAST
LIMIT 1
"""

_GRANICUS_JOB_STATUSES: tuple[str, ...] = ("pending", "claimed", "completed", "failed")


@router.get("/health/crawlers", response_model=CrawlerHealthResponse)
def crawler_health(conn=Depends(get_db_conn)) -> CrawlerHealthResponse:
    # Same try/except-then-500 shape as app/api/sources.py's get_source:
    # a DB error here must return this API's usual {"detail": ...} JSON
    # shape and log through this module's own logger, not fall through to
    # Starlette's default plain-text handler.
    try:
        with conn.cursor() as cur:
            cur.execute(_LATEST_PER_SOURCE_SQL)
            latest_rows = cur.fetchall()

            cur.execute(_GRANICUS_STATUS_COUNTS_SQL)
            granicus_count_rows = cur.fetchall()

            cur.execute(_GRANICUS_LATEST_FAILURE_SQL)
            granicus_failure_row = cur.fetchone()
    except Exception as exc:
        logger.error("crawler health check: database error: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again later.",
        ) from exc

    latest_by_source = {
        row[0]: dict(zip(_SOURCE_STATUS_COLUMNS, row)) for row in latest_rows
    }

    sources: list[CrawlerSourceStatus] = []
    for source_name in TIER1_SOURCE_NAMES:
        row = latest_by_source.get(source_name)
        if row is None:
            sources.append(
                CrawlerSourceStatus(
                    source_name=source_name,
                    status="never_run",
                    last_started_at=None,
                    last_finished_at=None,
                    inserted=None,
                    skipped=None,
                    chunks_failed=None,
                    error_message=None,
                )
            )
            continue
        sources.append(
            CrawlerSourceStatus(
                source_name=source_name,
                # outcome is a DB CHECK-constrained 'success'|'failed'
                # (app/db/crawl_runs.py) — anything not literally
                # 'success' reads as failed, never silently defaulted to
                # ok.
                status="ok" if row["outcome"] == "success" else "failed",
                last_started_at=row["started_at"],
                last_finished_at=row["finished_at"],
                inserted=row["inserted"],
                skipped=row["skipped"],
                chunks_failed=row["chunks_failed"],
                error_message=row["error_message"],
            )
        )

    counts = {status: 0 for status in _GRANICUS_JOB_STATUSES}
    total_jobs = 0
    for status_value, count in granicus_count_rows:
        # total_jobs is summed from every row returned, not from
        # counts.values(), specifically so an out-of-vocabulary status
        # (should be impossible given the table's own CHECK constraint,
        # app/granicus/schema.py — but this endpoint must fail loud, not
        # silently drop rows, per .claude/rules/crawler.md) still counts
        # toward the total even though it can't be broken out into one of
        # the four named buckets below (api-review pre-commit finding).
        total_jobs += count
        if status_value in counts:
            counts[status_value] = count
        else:
            logger.warning(
                "crawler health: granicus_transcription_jobs has unexpected "
                "status %r (%d row(s)) outside expected vocabulary %r — "
                "counted in total_jobs but not broken out by status",
                status_value,
                count,
                _GRANICUS_JOB_STATUSES,
            )

    if total_jobs == 0:
        granicus_status = "never_run"
    elif counts["failed"] > 0:
        granicus_status = "failed"
    else:
        granicus_status = "ok"

    granicus = GranicusHealthStatus(
        status=granicus_status,
        total_jobs=total_jobs,
        pending=counts["pending"],
        claimed=counts["claimed"],
        completed=counts["completed"],
        failed=counts["failed"],
        most_recent_failure_mp3_url=granicus_failure_row[0] if granicus_failure_row else None,
        most_recent_failure_meeting_title=(
            granicus_failure_row[1] if granicus_failure_row else None
        ),
        most_recent_failure_reason=granicus_failure_row[2] if granicus_failure_row else None,
    )

    return CrawlerHealthResponse(
        checked_at=datetime.now(timezone.utc),
        sources=sources,
        granicus=granicus,
    )
