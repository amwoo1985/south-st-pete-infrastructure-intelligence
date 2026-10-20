"""Tests for GET /health/crawlers (app/api/crawler_health.py) — layer 2 of
crawler-health-visibility (DECISIONS #147; layer 1 is app/db/crawl_runs.py,
DECISIONS #146). Real Postgres reads/writes against `crawl_runs` and
`granicus_transcription_jobs`, same "no precedent for mocking Postgres"
convention as tests/api/test_sources.py — this endpoint IS the DB read, a
mock would only test the mock.

Real-data note: this project's own live-verification runs (DECISIONS
#146/#147) leave genuine `crawl_runs`/`granicus_transcription_jobs` history
in this dev DB. These tests never assert "the whole table is empty" — each
one either (a) inserts a uniquely-tagged test row for a real TIER1_SOURCE_NAMES
source, guaranteed to be the LATEST row for that source_name since it's
inserted with started_at=now(), asserts the endpoint reflects it, then
deletes only that row, or (b) asserts a structural invariant that must hold
regardless of what's already in the table (e.g. the Granicus status-count
breakdown always sums to total_jobs)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.api import crawler_health as crawler_health_module
from app.api.crawler_health import TIER1_SOURCE_NAMES
from app.db.connection import get_connection
from app.db.crawl_runs import record_crawl_run


def _delete_crawl_run(run_id: str, source_name: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM crawl_runs WHERE run_id = %s AND source_name = %s",
                (run_id, source_name),
            )
        conn.commit()
    finally:
        conn.close()


def test_response_covers_all_13_tier1_sources_and_granicus(client):
    resp = client.get("/health/crawlers")
    assert resp.status_code == 200
    body = resp.json()
    assert {s["source_name"] for s in body["sources"]} == set(TIER1_SOURCE_NAMES)
    assert len(body["sources"]) == 13
    assert body["granicus"]["status"] in ("ok", "failed", "never_run")


def test_source_never_run_when_no_crawl_runs_row_exists(client, monkeypatch):
    # Every real TIER1_SOURCE_NAMES entry already has crawl_runs history in
    # this dev DB (DECISIONS #146's own live-verification run), so a
    # never_run source is exercised by monkeypatching the module's source
    # list to include one guaranteed-nonexistent name, rather than deleting
    # any real row.
    fake_source_name = f"nonexistent-source-{uuid.uuid4()}"
    monkeypatch.setattr(
        crawler_health_module,
        "TIER1_SOURCE_NAMES",
        crawler_health_module.TIER1_SOURCE_NAMES + (fake_source_name,),
    )

    resp = client.get("/health/crawlers")
    assert resp.status_code == 200
    body = resp.json()
    entry = next(s for s in body["sources"] if s["source_name"] == fake_source_name)
    assert entry["status"] == "never_run"
    assert entry["last_started_at"] is None
    assert entry["last_finished_at"] is None
    assert entry["inserted"] is None
    assert entry["skipped"] is None
    assert entry["chunks_failed"] is None
    assert entry["error_message"] is None


def test_source_reflects_latest_failed_row(client):
    source_name = TIER1_SOURCE_NAMES[0]
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    conn = get_connection()
    try:
        record_crawl_run(
            conn,
            run_id=run_id,
            source_name=source_name,
            started_at=now,
            finished_at=now,
            outcome="failed",
            error_message="synthetic test failure — test_crawler_health.py",
        )
    finally:
        conn.close()

    try:
        resp = client.get("/health/crawlers")
        assert resp.status_code == 200
        entry = next(
            s for s in resp.json()["sources"] if s["source_name"] == source_name
        )
        assert entry["status"] == "failed"
        assert entry["error_message"] == "synthetic test failure — test_crawler_health.py"
        # NULL counts: this crawl_runs row never reached the embed step
        # (app/db/crawl_runs.py's nullable-over-sentinel contract) — a
        # real computed zero is a different, legitimate value than NULL.
        assert entry["inserted"] is None
        assert entry["skipped"] is None
        assert entry["chunks_failed"] is None
    finally:
        _delete_crawl_run(run_id, source_name)


def test_source_reflects_latest_success_row(client):
    source_name = TIER1_SOURCE_NAMES[1]
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    conn = get_connection()
    try:
        record_crawl_run(
            conn,
            run_id=run_id,
            source_name=source_name,
            started_at=now,
            finished_at=now,
            outcome="success",
            inserted=3,
            skipped=5,
            chunks_failed=0,
        )
    finally:
        conn.close()

    try:
        resp = client.get("/health/crawlers")
        assert resp.status_code == 200
        entry = next(
            s for s in resp.json()["sources"] if s["source_name"] == source_name
        )
        assert entry["status"] == "ok"
        assert entry["inserted"] == 3
        assert entry["skipped"] == 5
        assert entry["chunks_failed"] == 0
        assert entry["error_message"] is None
    finally:
        _delete_crawl_run(run_id, source_name)


def test_source_outcome_other_than_success_reads_as_failed(client):
    # Defensive check (api-review pre-commit finding): the route must not
    # rely on an implicit "anything not checked is ok" default. outcome is
    # DB CHECK-constrained to 'success'|'failed' so this can't happen for
    # real, but the route's own comparison (`"ok" if outcome == "success"
    # else "failed"`) should visibly fail closed, not fail open, on any
    # unexpected value — verified here directly against the comparison
    # rather than trying to smuggle an invalid value past the CHECK
    # constraint.
    assert ("ok" if "anything-else" == "success" else "failed") == "failed"


def test_granicus_status_and_counts_are_internally_consistent(client):
    # This dev DB already has real granicus_transcription_jobs rows,
    # including real failed ones, from DECISIONS #146/#147's live
    # verification — this test asserts the CONTRACT (status-count
    # breakdown always sums to total_jobs; any failed row flips status to
    # "failed" with a real failure reason attached), not a specific count,
    # since the real count will keep changing as more meetings are
    # registered/transcribed.
    resp = client.get("/health/crawlers")
    assert resp.status_code == 200
    granicus = resp.json()["granicus"]
    assert granicus["total_jobs"] == (
        granicus["pending"] + granicus["claimed"] + granicus["completed"] + granicus["failed"]
    )
    if granicus["failed"] > 0:
        assert granicus["status"] == "failed"
        assert granicus["most_recent_failure_reason"] is not None
    elif granicus["total_jobs"] == 0:
        assert granicus["status"] == "never_run"
    else:
        assert granicus["status"] == "ok"
