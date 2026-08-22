"""Tests for app/granicus/worker.py — the Day 5+ claim/download/
transcribe/store pipeline (DECISIONS #12, #79-81).

No live network call and no live OpenAI call anywhere in this file:
- archive-video.granicus.com is mocked via `responses` (DECISIONS #23's
  established pattern) — including a robots.txt fixture for that host
  (none was recorded live for it, unlike Legistar's DECISIONS #16; tests
  register a synthetic 404-permissive robots.txt via
  tests.conftest.register_robots_permissive, the same convenience other
  crawler tests use for hosts without a recorded live check).
- OpenAI is mocked via a MagicMock client (tests/embeddings/test_client.py's
  established pattern) — no real API key, no real HTTP to api.openai.com.

DB-touching tests run against the REAL local docker-compose Postgres via
tests/granicus/conftest.py's db_conn fixture (rollback-at-teardown for
anything that stays within one uncommitted transaction). Functions in
worker.py commit internally by design (claim/mark_completed/mark_failed
each own a transaction boundary, matching register.py's `commit=True`
default) — any test that exercises a function past its first internal
commit() cannot rely on rollback-at-teardown alone and explicitly DELETEs
its synthetic row in a `finally` block instead. This is NOT a live
network/API call — it is the same real local dev Postgres every other
test in this suite already uses.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx2
import openai
import pytest
import requests
import responses

from app.crawlers.base import BaseCrawler, CrawlerStructureError
from app.db.connection import get_connection
from app.granicus.worker import (
    GRANICUS_CDN_REFERER,
    GRANICUS_CDN_USER_AGENT,
    WHISPER_MAX_FILE_BYTES,
    AudioTooLargeError,
    ProcessResult,
    TranscriptionValidationError,
    _download_ranged,
    _transcribe_audio,
    claim_next_job,
    process_one_job,
    process_pending_jobs,
    recover_stale_claims,
)
from tests.conftest import register_robots_permissive
from tests.granicus.conftest import insert_synthetic_job

MP3_URL = "https://archive-video.granicus.com/stpete/test-meeting.mp3"
_REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/audio/transcriptions")


def _crawler() -> BaseCrawler:
    return BaseCrawler(source_name="test-granicus-worker", min_request_interval_seconds=0)


def _fake_openai_client(**create_kwargs) -> MagicMock:
    client = MagicMock()
    client.audio.transcriptions.create = MagicMock(**create_kwargs)
    return client


def _delete_job(mp3_url: str) -> None:
    """Cleanup for tests that exercise a function past its first internal
    commit() — see module docstring. A fresh connection, not db_conn,
    since db_conn's own rollback can't undo an already-committed write."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM granicus_transcription_jobs WHERE mp3_url = %s", (mp3_url,))
        conn.commit()
    finally:
        conn.close()


# --- Claiming ----------------------------------------------------------------


def test_claim_next_job_returns_none_when_no_pending(db_conn):
    assert claim_next_job(db_conn, commit=False) is None


def test_claim_next_job_claims_oldest_pending_by_published_date(db_conn):
    insert_synthetic_job(db_conn, "https://archive-video.granicus.com/stpete/newer.mp3", published_date=date(2026, 3, 1))
    insert_synthetic_job(db_conn, "https://archive-video.granicus.com/stpete/older.mp3", published_date=date(2026, 1, 1))

    claimed = claim_next_job(db_conn, commit=False)

    assert claimed is not None
    assert claimed.mp3_url == "https://archive-video.granicus.com/stpete/older.mp3"
    assert claimed.published_date == date(2026, 1, 1)


def test_claim_next_job_sets_status_and_claimed_at(db_conn):
    url = "https://archive-video.granicus.com/stpete/to-claim.mp3"
    insert_synthetic_job(db_conn, url, published_date=date(2026, 1, 1))

    claim_next_job(db_conn, commit=False)

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, claimed_at FROM granicus_transcription_jobs WHERE mp3_url = %s", (url,))
        status, claimed_at = cur.fetchone()
    assert status == "claimed"
    assert claimed_at is not None


def test_claim_next_job_skip_locked_concurrent_safety(db_conn):
    """Narrow proof of the FOR UPDATE SKIP LOCKED shape: a row locked
    (claimed but not yet committed) by one connection must be skipped —
    not blocked on, not double-claimed — by a second connection's claim
    call, which should claim the other available pending row instead."""
    locked_url = "https://archive-video.granicus.com/stpete/skiplock-locked.mp3"
    free_url = "https://archive-video.granicus.com/stpete/skiplock-free.mp3"
    conn2 = get_connection()
    try:
        # Rows must be committed for a genuine cross-connection lock test
        # (an uncommitted insert on db_conn is invisible to conn2 at all,
        # locked or not) — unlike every other test in this file, this one
        # can't rely on rollback-at-teardown for cleanup.
        insert_synthetic_job(db_conn, locked_url, published_date=date(2026, 1, 1))
        insert_synthetic_job(db_conn, free_url, published_date=date(2026, 1, 2))
        db_conn.commit()

        claimed_by_conn1 = claim_next_job(db_conn, commit=False)  # holds the lock, uncommitted
        assert claimed_by_conn1.mp3_url == locked_url

        claimed_by_conn2 = claim_next_job(conn2)
        assert claimed_by_conn2.mp3_url == free_url
    finally:
        db_conn.rollback()
        conn2.rollback()
        conn2.close()
        _delete_job(locked_url)
        _delete_job(free_url)


# --- Stale-row recovery --------------------------------------------------------


def test_recover_stale_claims_reclaims_old_claimed_row(db_conn):
    url = "https://archive-video.granicus.com/stpete/stale.mp3"
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    insert_synthetic_job(db_conn, url, status="claimed", claimed_at=now - timedelta(hours=2))

    reclaimed = recover_stale_claims(db_conn, now=now, commit=False)

    assert reclaimed == [url]
    with db_conn.cursor() as cur:
        cur.execute("SELECT status, claimed_at FROM granicus_transcription_jobs WHERE mp3_url = %s", (url,))
        status, claimed_at = cur.fetchone()
    assert status == "pending"
    assert claimed_at is None


def test_recover_stale_claims_does_not_reclaim_recent_claimed_row(db_conn):
    url = "https://archive-video.granicus.com/stpete/recent.mp3"
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    insert_synthetic_job(db_conn, url, status="claimed", claimed_at=now - timedelta(minutes=10))

    reclaimed = recover_stale_claims(db_conn, now=now, commit=False)

    assert reclaimed == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM granicus_transcription_jobs WHERE mp3_url = %s", (url,))
        (status,) = cur.fetchone()
    assert status == "claimed"


def test_recover_stale_claims_ignores_pending_and_completed_rows(db_conn):
    pending_url = "https://archive-video.granicus.com/stpete/still-pending.mp3"
    completed_url = "https://archive-video.granicus.com/stpete/already-done.mp3"
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    ancient = now - timedelta(days=30)
    insert_synthetic_job(db_conn, pending_url, status="pending")
    insert_synthetic_job(db_conn, completed_url, status="completed", claimed_at=ancient, transcript_text="x")

    reclaimed = recover_stale_claims(db_conn, now=now, commit=False)

    assert reclaimed == []


# --- Range-chunked download ----------------------------------------------------


@responses.activate
def test_download_ranged_assembles_multiple_chunks(tmp_path):
    register_robots_permissive(responses, host="archive-video.granicus.com")
    chunk1, chunk2, chunk3 = b"AAAAAAAA", b"BBBBBBBB", b"CCCC"
    total = len(chunk1) + len(chunk2) + len(chunk3)
    responses.add(
        responses.GET, MP3_URL, body=chunk1, status=206,
        headers={"Content-Range": f"bytes 0-{len(chunk1) - 1}/{total}"},
    )
    responses.add(
        responses.GET, MP3_URL, body=chunk2, status=206,
        headers={"Content-Range": f"bytes {len(chunk1)}-{len(chunk1) + len(chunk2) - 1}/{total}"},
    )
    responses.add(
        responses.GET, MP3_URL, body=chunk3, status=206,
        headers={"Content-Range": f"bytes {len(chunk1) + len(chunk2)}-{total - 1}/{total}"},
    )

    dest = tmp_path / "audio.mp3"
    downloaded = _download_ranged(_crawler(), MP3_URL, dest, chunk_size=8, max_bytes=1_000_000)

    assert downloaded == total
    assert dest.read_bytes() == chunk1 + chunk2 + chunk3


@responses.activate
def test_download_ranged_aborts_after_first_chunk_when_oversized(tmp_path):
    register_robots_permissive(responses, host="archive-video.granicus.com")
    huge_total = 999_999_999
    responses.add(
        responses.GET, MP3_URL, body=b"X" * 8, status=206,
        headers={"Content-Range": f"bytes 0-7/{huge_total}"},
    )

    dest = tmp_path / "audio.mp3"
    with pytest.raises(AudioTooLargeError):
        _download_ranged(_crawler(), MP3_URL, dest, chunk_size=8, max_bytes=25_000_000)

    # robots.txt (1) + exactly one ranged probe chunk (1) — proves the
    # early-abort design never fetches a second chunk once the first
    # Content-Range reveals the file is over the Whisper limit.
    assert len(responses.calls) == 2


@responses.activate
def test_download_ranged_raises_structure_error_on_non_206(tmp_path):
    register_robots_permissive(responses, host="archive-video.granicus.com")
    responses.add(responses.GET, MP3_URL, body=b"nope", status=200)

    with pytest.raises(CrawlerStructureError):
        _download_ranged(_crawler(), MP3_URL, tmp_path / "audio.mp3", chunk_size=8)


@responses.activate
def test_download_ranged_raises_structure_error_on_missing_content_range(tmp_path):
    register_robots_permissive(responses, host="archive-video.granicus.com")
    responses.add(responses.GET, MP3_URL, body=b"somedata", status=206)

    with pytest.raises(CrawlerStructureError):
        _download_ranged(_crawler(), MP3_URL, tmp_path / "audio.mp3", chunk_size=8)


@responses.activate
def test_download_ranged_sends_cdn_user_agent_and_referer_and_range_header(tmp_path):
    register_robots_permissive(responses, host="archive-video.granicus.com")
    responses.add(
        responses.GET, MP3_URL, body=b"AAAAAAAA", status=206,
        headers={"Content-Range": "bytes 0-7/8"},
    )

    _download_ranged(_crawler(), MP3_URL, tmp_path / "audio.mp3", chunk_size=8)

    chunk_call = responses.calls[-1]
    assert chunk_call.request.headers["User-Agent"] == GRANICUS_CDN_USER_AGENT
    assert chunk_call.request.headers["Referer"] == GRANICUS_CDN_REFERER
    assert chunk_call.request.headers["Range"] == "bytes=0-7"


@responses.activate
def test_download_ranged_raises_structure_error_on_premature_empty_body(tmp_path):
    """A 206 with an empty body before `downloaded` has reached the
    announced `total` must fail loud, never be silently treated as
    "download complete" — this is the exact silent-truncation shape
    crawler-review flagged: a CDN/proxy returning an empty body on a 206
    without requests itself raising is a real, documented edge case."""
    register_robots_permissive(responses, host="archive-video.granicus.com")
    responses.add(
        responses.GET, MP3_URL, body=b"AAAAAAAA", status=206,
        headers={"Content-Range": "bytes 0-7/20"},
    )
    responses.add(
        responses.GET, MP3_URL, body=b"", status=206,
        headers={"Content-Range": "bytes 8-15/20"},
    )

    with pytest.raises(CrawlerStructureError):
        _download_ranged(_crawler(), MP3_URL, tmp_path / "audio.mp3", chunk_size=8, max_bytes=1_000_000)


@responses.activate
def test_download_ranged_retries_transient_connection_error_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr("app.granicus.worker.time.sleep", lambda _s: None)
    register_robots_permissive(responses, host="archive-video.granicus.com")
    responses.add(responses.GET, MP3_URL, body=requests.exceptions.ConnectionError("transient blip"))
    responses.add(
        responses.GET, MP3_URL, body=b"AAAAAAAA", status=206,
        headers={"Content-Range": "bytes 0-7/8"},
    )

    downloaded = _download_ranged(
        _crawler(), MP3_URL, tmp_path / "audio.mp3", chunk_size=8, max_bytes=1_000_000
    )

    assert downloaded == 8


@responses.activate
def test_download_ranged_does_not_retry_non_transient_http_error(tmp_path):
    register_robots_permissive(responses, host="archive-video.granicus.com")
    responses.add(responses.GET, MP3_URL, status=404)

    with pytest.raises(requests.exceptions.HTTPError):
        _download_ranged(_crawler(), MP3_URL, tmp_path / "audio.mp3", chunk_size=8, max_bytes=1_000_000)

    # robots.txt (1) + exactly one failed chunk attempt (1) — a 404 is a
    # client error, not transient, and must not be retried.
    assert len(responses.calls) == 2


# --- Whisper transcription -----------------------------------------------------


def test_transcribe_audio_success(tmp_path):
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"fake audio bytes")
    client = _fake_openai_client(return_value=SimpleNamespace(text="hello meeting transcript"))

    result = _transcribe_audio(client, audio_path)

    assert result == "hello meeting transcript"
    client.audio.transcriptions.create.assert_called_once()
    _, kwargs = client.audio.transcriptions.create.call_args
    assert kwargs["model"] == "whisper-1"


def test_transcribe_audio_retries_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr("app.granicus.worker.time.sleep", lambda _s: None)
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"fake audio bytes")
    rate_limit_response = httpx2.Response(status_code=429, request=_REQUEST)
    client = _fake_openai_client(
        side_effect=[
            openai.RateLimitError("rate limited", response=rate_limit_response, body=None),
            SimpleNamespace(text="ok after retry"),
        ]
    )

    result = _transcribe_audio(client, audio_path, max_attempts=3)

    assert result == "ok after retry"
    assert client.audio.transcriptions.create.call_count == 2


def test_transcribe_audio_exhausts_retries_and_raises_original(tmp_path, monkeypatch):
    monkeypatch.setattr("app.granicus.worker.time.sleep", lambda _s: None)
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"fake audio bytes")
    rate_limit_response = httpx2.Response(status_code=429, request=_REQUEST)
    err = openai.RateLimitError("rate limited", response=rate_limit_response, body=None)
    client = _fake_openai_client(side_effect=[err, err, err])

    with pytest.raises(openai.RateLimitError):
        _transcribe_audio(client, audio_path, max_attempts=3)

    assert client.audio.transcriptions.create.call_count == 3


def test_transcribe_audio_non_retryable_fails_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr("app.granicus.worker.time.sleep", lambda _s: None)
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"fake audio bytes")
    bad_request_response = httpx2.Response(status_code=400, request=_REQUEST)
    client = _fake_openai_client(
        side_effect=openai.BadRequestError("unsupported format", response=bad_request_response, body=None)
    )

    with pytest.raises(openai.BadRequestError):
        _transcribe_audio(client, audio_path, max_attempts=3)

    client.audio.transcriptions.create.assert_called_once()


def test_transcribe_audio_raises_audio_too_large_before_calling_api(tmp_path):
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"x" * (WHISPER_MAX_FILE_BYTES + 1))
    client = _fake_openai_client()

    with pytest.raises(AudioTooLargeError):
        _transcribe_audio(client, audio_path)

    client.audio.transcriptions.create.assert_not_called()


def test_transcribe_audio_raises_validation_error_on_missing_text_attribute(tmp_path):
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"fake audio bytes")
    client = _fake_openai_client(return_value=SimpleNamespace())

    with pytest.raises(TranscriptionValidationError):
        _transcribe_audio(client, audio_path)


def test_transcribe_audio_raises_validation_error_on_empty_text(tmp_path):
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"fake audio bytes")
    client = _fake_openai_client(return_value=SimpleNamespace(text="   "))

    with pytest.raises(TranscriptionValidationError):
        _transcribe_audio(client, audio_path)


# --- process_one_job: full pipeline against the real (cleaned-up) DB ----------


@responses.activate
def test_process_one_job_happy_path_end_to_end(db_conn, tmp_path):
    mp3_url = "https://archive-video.granicus.com/stpete/e2e-happy.mp3"
    register_robots_permissive(responses, host="archive-video.granicus.com")
    audio_bytes = b"FAKE-AUDIO-BYTES-FOR-TEST"
    responses.add(
        responses.GET, mp3_url, body=audio_bytes, status=206,
        headers={"Content-Range": f"bytes 0-{len(audio_bytes) - 1}/{len(audio_bytes)}"},
    )
    openai_client = _fake_openai_client(return_value=SimpleNamespace(text="this is the transcript"))
    crawler = _crawler()

    try:
        insert_synthetic_job(
            db_conn, mp3_url, meeting_title="TEST SYNTHETIC — e2e happy path", published_date=date(2026, 1, 1)
        )
        db_conn.commit()

        result = process_one_job(db_conn, openai_client, crawler, tmp_dir=tmp_path)

        assert result == ProcessResult(mp3_url=mp3_url, status="completed")
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT status, transcript_text, claimed_at FROM granicus_transcription_jobs WHERE mp3_url = %s",
                (mp3_url,),
            )
            status, transcript_text, claimed_at = cur.fetchone()
        assert status == "completed"
        assert transcript_text == "this is the transcript"
        assert claimed_at is not None  # left set on terminal states, see worker.py docstring
        assert list(tmp_path.glob("granicus-*.mp3")) == []  # temp file cleaned up
    finally:
        _delete_job(mp3_url)


def test_process_one_job_returns_none_when_no_pending(db_conn, tmp_path):
    result = process_one_job(db_conn, _fake_openai_client(), _crawler(), tmp_dir=tmp_path)
    assert result is None


@responses.activate
def test_process_one_job_marks_failed_on_oversized_audio(db_conn, tmp_path):
    mp3_url = "https://archive-video.granicus.com/stpete/e2e-oversized.mp3"
    register_robots_permissive(responses, host="archive-video.granicus.com")
    responses.add(
        responses.GET, mp3_url, body=b"X" * 8, status=206,
        headers={"Content-Range": "bytes 0-7/999999999"},
    )
    openai_client = _fake_openai_client()
    crawler = _crawler()

    try:
        insert_synthetic_job(
            db_conn, mp3_url, meeting_title="TEST SYNTHETIC — e2e oversized", published_date=date(2026, 1, 1)
        )
        db_conn.commit()

        result = process_one_job(db_conn, openai_client, crawler, tmp_dir=tmp_path)

        assert result.status == "failed"
        assert result.failure_reason is not None
        openai_client.audio.transcriptions.create.assert_not_called()
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT status, failure_reason FROM granicus_transcription_jobs WHERE mp3_url = %s", (mp3_url,)
            )
            status, failure_reason = cur.fetchone()
        assert status == "failed"
        assert failure_reason is not None
    finally:
        _delete_job(mp3_url)


@responses.activate
def test_process_one_job_marks_failed_on_transcription_error(db_conn, tmp_path):
    mp3_url = "https://archive-video.granicus.com/stpete/e2e-transcribe-fail.mp3"
    register_robots_permissive(responses, host="archive-video.granicus.com")
    audio_bytes = b"SMALL-AUDIO"
    responses.add(
        responses.GET, mp3_url, body=audio_bytes, status=206,
        headers={"Content-Range": f"bytes 0-{len(audio_bytes) - 1}/{len(audio_bytes)}"},
    )
    bad_request_response = httpx2.Response(status_code=400, request=_REQUEST)
    openai_client = _fake_openai_client(
        side_effect=openai.BadRequestError("unsupported format", response=bad_request_response, body=None)
    )
    crawler = _crawler()

    try:
        insert_synthetic_job(
            db_conn, mp3_url, meeting_title="TEST SYNTHETIC — e2e transcribe fail", published_date=date(2026, 1, 1)
        )
        db_conn.commit()

        result = process_one_job(db_conn, openai_client, crawler, tmp_dir=tmp_path)

        assert result.status == "failed"
        assert "transcription failed" in result.failure_reason
    finally:
        _delete_job(mp3_url)


def test_process_pending_jobs_stops_when_queue_empty(db_conn, tmp_path):
    results = process_pending_jobs(db_conn, _fake_openai_client(), _crawler(), max_jobs=5, tmp_dir=tmp_path)
    assert results == []


@responses.activate
def test_process_pending_jobs_aborts_batch_on_systemic_auth_error(db_conn, tmp_path):
    """A systemic OpenAI auth failure on job 1 must abort the whole batch
    before job 2 is ever claimed (DECISIONS #74's shape) — not fail job 1
    and move on to independently fail job 2 against the same broken
    credential. Job 1 itself gets reverted to 'pending' (not permanently
    'failed'), since the credential — not the job — is what's broken; see
    _revert_to_pending's docstring for the reasoning."""
    url1 = "https://archive-video.granicus.com/stpete/auth-abort-1.mp3"
    url2 = "https://archive-video.granicus.com/stpete/auth-abort-2.mp3"
    register_robots_permissive(responses, host="archive-video.granicus.com")
    audio_bytes = b"SMALL-AUDIO"
    # Only job 1 (older published_date, claimed first) should ever reach
    # a download call — no response is registered for url2 at all, so if
    # the batch wrongly proceeded to job 2 this test would fail loudly
    # with a mismatched-mock error rather than silently passing.
    responses.add(
        responses.GET, url1, body=audio_bytes, status=206,
        headers={"Content-Range": f"bytes 0-{len(audio_bytes) - 1}/{len(audio_bytes)}"},
    )
    auth_response = httpx2.Response(status_code=401, request=_REQUEST)
    openai_client = _fake_openai_client(
        side_effect=openai.AuthenticationError("bad key", response=auth_response, body=None)
    )
    crawler = _crawler()

    try:
        insert_synthetic_job(
            db_conn, url1, meeting_title="TEST SYNTHETIC — auth abort 1", published_date=date(2026, 1, 1)
        )
        insert_synthetic_job(
            db_conn, url2, meeting_title="TEST SYNTHETIC — auth abort 2", published_date=date(2026, 1, 2)
        )
        db_conn.commit()

        with pytest.raises(openai.AuthenticationError):
            process_pending_jobs(db_conn, openai_client, crawler, max_jobs=5, tmp_dir=tmp_path)

        assert openai_client.audio.transcriptions.create.call_count == 1

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT mp3_url, status, claimed_at FROM granicus_transcription_jobs "
                "WHERE mp3_url IN (%s, %s)",
                (url1, url2),
            )
            rows = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

        # job 1 hit the doomed API call: reverted to 'pending', not
        # permanently 'failed' -- the credential, not the job, is broken.
        assert rows[url1] == ("pending", None)
        # job 2 was never claimed at all -- the batch aborted before it.
        assert rows[url2] == ("pending", None)
    finally:
        _delete_job(url1)
        _delete_job(url2)
