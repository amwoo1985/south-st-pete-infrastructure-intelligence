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
from app.granicus.audio_split import AudioSplitResult, SplitPiece
from app.granicus.worker import (
    GRANICUS_CDN_REFERER,
    GRANICUS_CDN_USER_AGENT,
    MAX_TOTAL_DOWNLOAD_BYTES,
    STALE_CLAIM_THRESHOLD,
    WHISPER_MAX_FILE_BYTES,
    AudioTooLargeError,
    ClaimedJob,
    PieceTranscriptionError,
    ProcessResult,
    TranscriptionValidationError,
    _download_ranged,
    _transcribe_audio,
    _transcribe_possibly_split,
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
    # 5h > STALE_CLAIM_THRESHOLD (4h, re-derived per worker.py's module
    # docstring for the duration-aware-split design) — must be older than
    # the real threshold to exercise reclaim, not the pre-split-design 1h.
    insert_synthetic_job(db_conn, url, status="claimed", claimed_at=now - timedelta(hours=5))

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


def test_recover_stale_claims_does_not_reclaim_realistic_long_job_still_in_flight(db_conn):
    """A real ~5hr/250MB meeting's realistic worst-case pipeline time is
    ~92 minutes (worker.py module docstring's re-derived scenario A') —
    3.5h is comfortably within that job still being honestly in-flight
    and must NOT be reclaimed out from under it, pinning the 4h threshold
    against a realistic near-boundary case, not just an arbitrary recent
    claim."""
    url = "https://archive-video.granicus.com/stpete/long-job-in-flight.mp3"
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    insert_synthetic_job(db_conn, url, status="claimed", claimed_at=now - timedelta(hours=3, minutes=30))

    reclaimed = recover_stale_claims(db_conn, now=now, commit=False)

    assert reclaimed == []


def test_stale_claim_threshold_value_matches_module_docstrings_worked_math():
    """Pins STALE_CLAIM_THRESHOLD's actual value against the re-derived
    worked math in worker.py's module docstring ("decision (c)" section)
    — a direct guard against the constant and its documented derivation
    silently drifting apart."""
    assert STALE_CLAIM_THRESHOLD == timedelta(hours=4)


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
def test_download_ranged_aborts_after_first_chunk_when_over_outer_ceiling(tmp_path):
    """The early-abort mechanism itself is unchanged by decision (c) — it
    just now guards MAX_TOTAL_DOWNLOAD_BYTES (the outer sanity ceiling),
    not WHISPER_MAX_FILE_BYTES (the per-piece ceiling, which no longer
    aborts the download at all — see
    test_download_ranged_downloads_fully_when_over_whisper_limit_but_under_outer_ceiling)."""
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
    # Content-Range reveals the file is over the given ceiling.
    assert len(responses.calls) == 2


@responses.activate
def test_download_ranged_downloads_fully_when_over_whisper_limit_but_under_outer_ceiling(tmp_path):
    """Core decision-(c) behavior change: a file over WHISPER_MAX_FILE_BYTES
    (25 MiB) but under MAX_TOTAL_DOWNLOAD_BYTES (500 MiB) must download to
    completion (to be split afterward), NOT abort early the way the old
    decision-(b) design did."""
    register_robots_permissive(responses, host="archive-video.granicus.com")
    chunk1, chunk2 = b"A" * 8, b"B" * 4
    total = len(chunk1) + len(chunk2)  # 12 bytes — "over Whisper's limit" is
    # simulated via a small max_bytes/max_piece_bytes-equivalent stand-in
    # (chunk_size=8 keeps this fast) rather than a real 25MB+ payload.
    responses.add(
        responses.GET, MP3_URL, body=chunk1, status=206,
        headers={"Content-Range": f"bytes 0-{len(chunk1) - 1}/{total}"},
    )
    responses.add(
        responses.GET, MP3_URL, body=chunk2, status=206,
        headers={"Content-Range": f"bytes {len(chunk1)}-{total - 1}/{total}"},
    )

    dest = tmp_path / "audio.mp3"
    # max_bytes stands in for MAX_TOTAL_DOWNLOAD_BYTES here (well above
    # `total`) while `total` itself stands in for "over WHISPER_MAX_FILE_BYTES"
    # — the point under test is purely "does it keep downloading past one
    # chunk when total > the OLD Whisper-sized ceiling but < the outer one."
    downloaded = _download_ranged(_crawler(), MP3_URL, dest, chunk_size=8, max_bytes=1_000_000)

    assert downloaded == total
    assert dest.read_bytes() == chunk1 + chunk2
    # robots.txt (1) + two ranged chunks (2) — proves it did NOT abort
    # after the first chunk the way the old per-piece-ceiling abort would.
    assert len(responses.calls) == 3


def test_max_total_download_bytes_value_matches_module_docstrings_worked_math():
    """Pins MAX_TOTAL_DOWNLOAD_BYTES against worker.py's module docstring
    ("decision (c)" section) — 500 MiB, ~2.2x DECISIONS #116's real
    observed max (250MB/5hr)."""
    assert MAX_TOTAL_DOWNLOAD_BYTES == 500 * 1024 * 1024


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


# --- _transcribe_possibly_split: split-aware transcription (decision (c)) ----
#
# No real pydub/ffmpeg decode here — `split_audio_for_whisper` itself is
# mocked (monkeypatched in app.granicus.worker's namespace, the same
# "mock the audio-processing... calls" convention this module's docstring
# specifies), matching how _download_ranged/_transcribe_audio are already
# mocked elsewhere in this file. ffmpeg isn't installed on this dev
# machine at all (see requirements.txt's pydub comment) — real
# split behavior is covered by tests/granicus/test_audio_split.py against
# a fake decoded-audio stand-in, and by this round's real end-to-end
# validation against the local docker-compose stack (which does have
# ffmpeg, added by deploy-infra).


def _job(mp3_url: str = MP3_URL) -> ClaimedJob:
    return ClaimedJob(
        mp3_url=mp3_url,
        meeting_title="TEST SYNTHETIC — split transcription",
        source_url="https://stpete.granicus.com/MediaPlayer.php?view_id=1&clip_id=999",
        published_date=date(2026, 1, 1),
    )


def _write_piece_files(tmp_path, n: int) -> list[SplitPiece]:
    pieces = []
    for i in range(1, n + 1):
        p = tmp_path / f"piece-{i}.mp3"
        p.write_bytes(b"x" * 10)
        pieces.append(SplitPiece(path=p, index=i))
    return pieces


def test_transcribe_possibly_split_single_call_when_under_limit(tmp_path):
    """The common-case path (most real meetings to date, DECISIONS #116)
    is completely unchanged: a file at/under WHISPER_MAX_FILE_BYTES never
    invokes the split module at all."""
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"small file")
    client = _fake_openai_client(return_value=SimpleNamespace(text="single-call transcript"))

    result = _transcribe_possibly_split(client, audio_path, _job(), tmp_path)

    assert result == "single-call transcript"
    client.audio.transcriptions.create.assert_called_once()


def test_transcribe_possibly_split_stitches_multiple_pieces(tmp_path, monkeypatch):
    """An oversized file splits into N pieces, each transcribed
    separately, and the results stitch into one transcript_text string in
    chronological order — proving the N-pieces-split-and-stitch-correctly
    case this round's brief calls out explicitly."""
    audio_path = tmp_path / "big-audio.mp3"
    audio_path.write_bytes(b"x" * (WHISPER_MAX_FILE_BYTES + 1))
    pieces = _write_piece_files(tmp_path, 3)
    split_result = AudioSplitResult(pieces=pieces, total_duration_seconds=1800.0)
    monkeypatch.setattr(
        "app.granicus.worker.split_audio_for_whisper", lambda *a, **k: split_result
    )
    client = _fake_openai_client(
        side_effect=[
            SimpleNamespace(text="piece one."),
            SimpleNamespace(text="piece two."),
            SimpleNamespace(text="piece three."),
        ]
    )

    result = _transcribe_possibly_split(client, audio_path, _job(), tmp_path)

    assert result == "piece one. piece two. piece three."
    assert client.audio.transcriptions.create.call_count == 3
    # Best-effort cleanup: all piece files removed after stitching.
    for piece in pieces:
        assert not piece.path.exists()


def test_transcribe_possibly_split_one_piece_failure_names_the_piece_and_fails_whole_job(
    tmp_path, monkeypatch
):
    """A single split piece's transcription ultimately failing (after
    _transcribe_audio's own retries are exhausted) must fail the WHOLE
    job loud, naming which piece failed and why — never silently drop
    that segment's text from a stitched result
    (.claude/rules/crawler.md's fail-loud rule, applied to a partial-job
    failure)."""
    monkeypatch.setattr("app.granicus.worker.time.sleep", lambda _s: None)
    audio_path = tmp_path / "big-audio.mp3"
    audio_path.write_bytes(b"x" * (WHISPER_MAX_FILE_BYTES + 1))
    pieces = _write_piece_files(tmp_path, 3)
    split_result = AudioSplitResult(pieces=pieces, total_duration_seconds=1800.0)
    monkeypatch.setattr(
        "app.granicus.worker.split_audio_for_whisper", lambda *a, **k: split_result
    )
    bad_request_response = httpx2.Response(status_code=400, request=_REQUEST)
    client = _fake_openai_client(
        side_effect=[
            SimpleNamespace(text="piece one."),
            openai.BadRequestError("unsupported format", response=bad_request_response, body=None),
            SimpleNamespace(text="never reached"),
        ]
    )

    with pytest.raises(PieceTranscriptionError) as exc_info:
        _transcribe_possibly_split(client, audio_path, _job(), tmp_path)

    assert "piece 2/3" in str(exc_info.value)
    assert "BadRequestError" in str(exc_info.value)
    # Piece 3 never reached — the loop stops at the first failure, and the
    # third mocked side_effect ("never reached") was never consumed.
    assert client.audio.transcriptions.create.call_count == 2
    # Best-effort cleanup still ran for every piece, including ones after
    # the failure point, despite the job failing.
    for piece in pieces:
        assert not piece.path.exists()


def test_transcribe_possibly_split_propagates_systemic_auth_error_unwrapped(tmp_path, monkeypatch):
    """A systemic OpenAI credential failure on any piece must propagate
    as the original exception type, unwrapped — process_one_job's
    systemic-vs-per-job handling depends on catching this exact type to
    revert (not permanently fail) the whole job."""
    audio_path = tmp_path / "big-audio.mp3"
    audio_path.write_bytes(b"x" * (WHISPER_MAX_FILE_BYTES + 1))
    pieces = _write_piece_files(tmp_path, 2)
    split_result = AudioSplitResult(pieces=pieces, total_duration_seconds=1200.0)
    monkeypatch.setattr(
        "app.granicus.worker.split_audio_for_whisper", lambda *a, **k: split_result
    )
    auth_response = httpx2.Response(status_code=401, request=_REQUEST)
    client = _fake_openai_client(
        side_effect=openai.AuthenticationError("bad key", response=auth_response, body=None)
    )

    with pytest.raises(openai.AuthenticationError):
        _transcribe_possibly_split(client, audio_path, _job(), tmp_path)


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
def test_process_one_job_marks_failed_on_audio_over_outer_ceiling(db_conn, tmp_path):
    """999,999,999 bytes is over MAX_TOTAL_DOWNLOAD_BYTES (500 MiB, the
    outer sanity ceiling — decision (c)), not just over
    WHISPER_MAX_FILE_BYTES (25 MiB, which no longer aborts the download at
    all) — this proves a genuine outlier still fails loud without ever
    reaching a download or a Whisper call."""
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
def test_process_one_job_completes_via_split_path_end_to_end(db_conn, tmp_path, monkeypatch):
    """Full process_one_job pipeline for a file over WHISPER_MAX_FILE_BYTES
    but under MAX_TOTAL_DOWNLOAD_BYTES: downloads fully (no early abort),
    splits (mocked — no real ffmpeg on this dev machine), transcribes
    each piece, stitches, and stores the completed job with the stitched
    transcript_text — the split-path analog of
    test_process_one_job_happy_path_end_to_end."""
    mp3_url = "https://archive-video.granicus.com/stpete/e2e-split.mp3"
    register_robots_permissive(responses, host="archive-video.granicus.com")
    # A real 25MB+ payload isn't needed to exercise this path — only the
    # downloaded file's on-disk SIZE (checked by _transcribe_possibly_split)
    # needs to exceed WHISPER_MAX_FILE_BYTES, and split_audio_for_whisper
    # itself is mocked below rather than really invoked.
    audio_bytes = b"X" * (WHISPER_MAX_FILE_BYTES + 1)
    responses.add(
        responses.GET, mp3_url, body=audio_bytes, status=206,
        headers={"Content-Range": f"bytes 0-{len(audio_bytes) - 1}/{len(audio_bytes)}"},
    )
    piece_paths = []
    for i in (1, 2):
        p = tmp_path / f"e2e-split-piece-{i}.mp3"
        p.write_bytes(b"x" * 10)
        piece_paths.append(p)
    split_result = AudioSplitResult(
        pieces=[SplitPiece(path=piece_paths[0], index=1), SplitPiece(path=piece_paths[1], index=2)],
        total_duration_seconds=3600.0,
    )
    monkeypatch.setattr(
        "app.granicus.worker.split_audio_for_whisper", lambda *a, **k: split_result
    )
    openai_client = _fake_openai_client(
        side_effect=[SimpleNamespace(text="first half."), SimpleNamespace(text="second half.")]
    )
    crawler = _crawler()

    try:
        insert_synthetic_job(
            db_conn, mp3_url, meeting_title="TEST SYNTHETIC — e2e split", published_date=date(2026, 1, 1)
        )
        db_conn.commit()

        result = process_one_job(db_conn, openai_client, crawler, tmp_dir=tmp_path)

        assert result == ProcessResult(mp3_url=mp3_url, status="completed")
        assert openai_client.audio.transcriptions.create.call_count == 2
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT status, transcript_text FROM granicus_transcription_jobs WHERE mp3_url = %s",
                (mp3_url,),
            )
            status, transcript_text = cur.fetchone()
        assert status == "completed"
        assert transcript_text == "first half. second half."
        for p in piece_paths:
            assert not p.exists()  # split-piece temp files cleaned up
        assert list(tmp_path.glob("granicus-*.mp3")) == []  # original download cleaned up too
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
