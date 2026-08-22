"""Granicus transcription job worker — Day 5+ (DECISIONS #12, #79-81).

Claims one `status='pending'` row from `granicus_transcription_jobs`
(app/granicus/schema.py), downloads its `mp3_url` from
`archive-video.granicus.com` in memory-bounded Range-chunked windows,
transcribes it via OpenAI `whisper-1`, and stores the result — or a
specific, diagnosable failure reason (`.claude/rules/crawler.md`: never
silently empty).

This module does NOT register meetings (app/granicus/register.py, already
built) and does NOT run an unattended full backfill — `process_pending_jobs`
below is a thin, explicitly-bounded batch wrapper for validating a small
batch (`.claude/rules/crawler.md`: "validate a small batch before letting
the full 12-month backfill run unattended"), not a default-on runner.


## Stale-claim recovery threshold: worked reasoning

`STALE_CLAIM_THRESHOLD = timedelta(hours=1)`. Derived, not a round-number
guess — two independent worst-case estimates, both well under an hour:

**A. Worst case under this module's actual built design.** The download
step (`_download_ranged`) fetches one Range-chunked probe (`RANGE_CHUNK_SIZE_BYTES`
= 8 MiB) first, reads the real total file size off that response's
`Content-Range` header, and aborts immediately — before fetching any more
of the file — if that total exceeds `WHISPER_MAX_FILE_BYTES` (25 MiB, see
below). Given a real multi-hour St. Petersburg City Council meeting MP3
is virtually certain to exceed 25 MiB (even a low 64 kbps mono
spoken-word encoding is ~28.8 MB/hour), the common case fails loud after
one bounded ~8 MiB fetch (bounded by `BaseCrawler`'s 30s request timeout
+ up to 2.0s rate-limit wait ≈ well under a minute) and never reaches
Whisper at all. The only case that reaches a full download + a Whisper
call is a short/atypical meeting whose file is *already* ≤ 25 MiB — at a
conservative 128 kbps (16 KB/s), that caps out at ~27 minutes of audio;
at a leaner 64 kbps mono, ~53 minutes. Call it "up to ~1 hour of audio,
≤25 MiB file" as the realistic ceiling for a job that actually runs the
full pipeline.
  - Download: ≤25 MiB in 8 MiB windows ≈ 4 ranged requests, each bounded
    at ~60s worst case (30s HTTP timeout + up to 2s rate-limit wait,
    rounded up for margin) ⇒ ≤ 4 min.
  - Transcribe: one `audio.transcriptions.create()` call, up to
    `max_attempts=3` attempts with exponential backoff (trivial — 1s +
    2s ≈ 3s total sleep). Whisper has no published hard per-request SLA
    to cite, so each attempt's wall-clock (upload + server processing +
    response) is bounded generously at 5 minutes ⇒ 3 attempts × 5 min +
    trivial backoff ≈ 15 min.
  - Total: ≈ 4 + 15 = **19 minutes** worst case under the actual design.

**B. Pessimistic cross-check, assuming the early-abort-on-first-chunk
design somehow didn't fire** (defense against my own design being wrong
in a way I haven't thought of) — download a full 6-hour meeting (a
plausible upper bound for a St. Pete council meeting with heavy public
comment) at a conservative 128 kbps ⇒ ~337.5 MB, ~43 chunks at 8 MiB
each. Real Range-chunked transfer time dominates over the 2.0s rate-limit
floor at this chunk size (8 MiB at a conservative 5 Mbps ≈ 13s/chunk);
generously bounding each chunk (transfer + rate-limit wait + margin) at
30s ⇒ 43 × 30s ≈ **21.5 minutes** to download the *entire* file even in
this scenario the design is meant to prevent. (No Whisper multi-part
multiplier applies here — decision (b) below means an oversized file
never reaches Whisper at all, it fails at the size check.)

Both estimates land at roughly 20-25 minutes. Setting the threshold at
**60 minutes** gives ~2.5-3x margin over either worst case: comfortably
would not reclaim a job still honestly in-flight, but reclaims a job
whose worker actually died (crashed process, killed container) within an
hour rather than letting a dead claim block that row indefinitely — short
enough that a dead worker doesn't stall the (currently small, manually
registered) queue for a full day.


## Range-chunk size: 8 MiB

Large enough to keep the number of HTTP round-trips (each paying the
2.0s/host rate-limit floor) reasonable — a ≤25 MiB file needs only ~4
requests. Small enough to keep peak resident memory for one in-flight
chunk trivial regardless of total file size, and comfortably under the
25 MiB Whisper ceiling itself, so a single chunk can never by itself
exceed that limit. `RANGE_CHUNK_SIZE_BYTES = 8 * 1024 * 1024`.


## >25 MiB Whisper case: decision (b) — fail loud, no chunked submission

Verified against the current OpenAI docs (developers.openai.com/api/docs/guides/speech-to-text,
fetched this session — platform.openai.com/docs/... 301-redirects there
now): whisper-1's real limit is "files can be up to 25 MB"; supported
formats are mp3/mp4/mpeg/mpga/m4a/wav/webm. Checked whether the installed
`openai==3.3.1` SDK offers a built-in multi-part/chunking helper before
assuming there wasn't one: `transcriptions.create()` does expose a
`chunking_strategy` parameter, but per the SDK's own docstring this
controls *server-side* VAD-based chunking of audio already within one
request/25 MiB, and is explicitly documented as ignored for `whisper-1`
("streaming is not supported for the whisper-1 model and will be
ignored" — chunking_strategy is a streaming-response knob). It does not
split a >25 MiB file across multiple requests. No SDK-provided out.

Built (b): fail loud with a specific `failure_reason`
(`AudioTooLargeError`, raised the moment the first Range chunk's
`Content-Range` total is known to exceed `WHISPER_MAX_FILE_BYTES` — see
`_download_ranged`), not (a) split-and-concatenate. A byte-offset split
of an MP3 stream is not guaranteed frame-safe (MP3 frames aren't
fixed-length; a raw cut can land mid-frame and corrupt both halves).
Real audio-duration-aware splitting needs an audio-processing dependency
(pydub, wrapping ffmpeg) — a new system binary in the Docker image with
real deploy implications squarely outside this module's and this round's
scope (deploy-infra's territory, not decided). Flagging
duration-aware chunked transcription as a named future follow-up
needing its own DECISIONS entry and Amber's sign-off on the ffmpeg/pydub
dependency, per this round's brief.

`WHISPER_MAX_FILE_BYTES = 25 * 1024 * 1024` (26,214,400 bytes — the MiB
reading of "25 MB", the commonly-reported actually-enforced byte ceiling;
I could not verify the exact byte-for-byte enforcement point against a
live API call in this environment, so this is the conservative
interpretation, not a confirmed-exact one).

**Whisper per-minute price** (verified live this session via OpenAI's
current pricing docs at developers.openai.com/api/docs/pricing, the
canonical page platform.openai.com/docs/pricing now redirects to):
Whisper transcription is **$0.006/minute** ($0.36/hour). Real cost
line — flag before any real batch run per `.claude/rules/crawler.md`.


## Failure classification for the Whisper call (mirrors
app/embeddings/client.py's `_RETRYABLE_EXCEPTIONS` shape exactly, per
.claude/rules/rag.md's "classify failures before retrying"):
retryable (bounded retry + exponential backoff): `openai.RateLimitError`,
`openai.APITimeoutError`, `openai.APIConnectionError`,
`openai.InternalServerError`. Not retryable (fail immediately, original
exception surfaces into `failure_reason`): everything else
(`openai.BadRequestError` — e.g. unsupported audio format,
`openai.AuthenticationError`, etc) — retrying a malformed request or a
bad API key will not fix it.


## `claimed_at` on terminal states: left set, not cleared

Both `_mark_completed` and `_mark_failed` leave `claimed_at` as-is rather
than nulling it out. There is no separate `completed_at`/`failed_at`
column in this table (DECISIONS #81) — `claimed_at` is the only
timestamp this schema has for "when did a worker last touch this row",
and it is useful audit/health-view information (CLAUDE.md's "at least
basic visibility... not silent failure" note) for a completed or failed
row, not just a pending/claimed one. Clearing it would only be cosmetic:
`recover_stale_claims`'s query filters on `status = 'claimed'`, so a
terminal-state row's `claimed_at` value never interacts with stale
recovery regardless of whether it's cleared. Keeping it costs nothing and
preserves information a future status view would want.


## Claim ordering: `published_date ASC`

Oldest meetings transcribe first. This project's retrieval use case (CBA
negotiation history) benefits from older meetings — the deeper backlog —
becoming searchable sooner rather than newest-registered-first, and
registration is human-paced (a few meetings at a time via
`scripts/register_granicus_meeting.py`), so there's no risk of a
fast-growing queue burying old meetings indefinitely under this order.
"""

from __future__ import annotations

import logging
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

import openai
import psycopg
import requests

from app.crawlers.base import BaseCrawler, CrawlerStructureError

logger = logging.getLogger("granicus.worker")

# --- Tunable constants (see module docstring for the worked reasoning) ----

STALE_CLAIM_THRESHOLD = timedelta(hours=1)
RANGE_CHUNK_SIZE_BYTES = 8 * 1024 * 1024  # 8 MiB
WHISPER_MODEL = "whisper-1"
WHISPER_MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MiB ("25 MB" per OpenAI docs)

# archive-video.granicus.com's CDN bot-filters requests without a
# browser-like User-Agent + Referer (.claude/rules/crawler.md, confirmed
# live 2026-08-19 — a bare `403 Request blocked`, not real access control).
# RESOLVED (2026-08-22, DECISIONS #89/#91): this host's robots.txt itself
# 403s on every User-Agent tried (honest UA, no UA, browser UA+Referer
# alike) — never a clean 404, never a real published policy. Amber's
# decision (#91): a narrow, host-specific exception in
# `app/crawlers/base.py`'s `RobotsChecker`
# (`ROBOTS_403_TREATED_AS_PERMISSIVE_HOSTS`) treats a 403 on THIS host's
# robots.txt fetch as "no restrictions declared", the same as a 404 would
# be — every other host's 403 (or any other non-404 error, on any host)
# still raises `RobotsError` exactly as before. This is the one place this
# codebase intentionally sends a non-honest User-Agent — deliberately
# scoped to only the ranged audio-download request, never to the robots.txt
# check itself, which stays honest (`BaseCrawler.fetch()`'s robots check
# always uses the crawler's own configured, honest DEFAULT_USER_AGENT;
# only the headers passed through **kwargs on the ranged download call
# itself are overridden).
GRANICUS_CDN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
GRANICUS_CDN_REFERER = "https://stpete.granicus.com/"

# Explicit column list for the claim query — .claude/rules/data.md bars
# `SELECT *`.
CLAIM_COLUMNS: tuple[str, ...] = ("mp3_url", "meeting_title", "source_url", "published_date")

_CONTENT_RANGE_RE = re.compile(r"bytes \d+-\d+/(\d+)")

# Classification for a single Range chunk fetch — same "classify before
# retrying" discipline as _TRANSCRIBE_RETRYABLE_EXCEPTIONS /
# app/embeddings/client.py: a connection blip or timeout is transient and
# worth a bounded retry; an HTTP client error (4xx — bad request, not
# found, forbidden) is not transient and retrying it won't help. A 5xx
# from the CDN IS treated as transient (server-side, plausibly momentary)
# and is retried. `requests.exceptions.ConnectionError`/`Timeout` are the
# network-layer failures `BaseCrawler.fetch()` can raise before ever
# getting a response to check a status code on.
_DOWNLOAD_RETRYABLE_NETWORK_EXCEPTIONS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)


def _is_retryable_download_error(exc: Exception) -> bool:
    if isinstance(exc, _DOWNLOAD_RETRYABLE_NETWORK_EXCEPTIONS):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        response = exc.response
        return response is not None and response.status_code >= 500
    return False


_TRANSCRIBE_RETRYABLE_EXCEPTIONS = (
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
)


class AudioTooLargeError(Exception):
    """Raised when the audio file exceeds whisper-1's single-request 25
    MiB limit. See module docstring, "decision (b)" — fails loud instead
    of splitting."""


class TranscriptionValidationError(ValueError):
    """Model output is untrusted input (.claude/rules/rag.md) — raised
    when the Whisper API response doesn't have the expected shape."""


@dataclass(frozen=True)
class ClaimedJob:
    mp3_url: str
    meeting_title: str
    source_url: str
    published_date: date


@dataclass(frozen=True)
class ProcessResult:
    mp3_url: str
    status: Literal["completed", "failed"]
    failure_reason: str | None = None


# --- Claiming ---------------------------------------------------------------


def claim_next_job(conn: psycopg.Connection, *, commit: bool = True) -> ClaimedJob | None:
    """Claims the oldest `status='pending'` row (by `published_date`,
    see module docstring) using `SELECT ... FOR UPDATE SKIP LOCKED` so
    concurrent workers never claim the same row or block on each other's
    locked-but-still-pending rows. Returns None if nothing is pending.

    The SELECT and the UPDATE that flips it to 'claimed' happen in the
    same transaction (the row lock from FOR UPDATE is held until
    `commit()`), so a concurrent claimer's SKIP LOCKED genuinely skips
    this row rather than racing it.

    `claimed_at` is set from app-side UTC `datetime.now(timezone.utc)`,
    matching `Attribution.now()`'s convention (app/crawlers/base.py) —
    the same timestamp-sourcing convention already used everywhere else
    in this codebase, not a second DB-side `NOW()` convention introduced
    here.
    """
    columns_sql = ", ".join(CLAIM_COLUMNS)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {columns_sql} FROM granicus_transcription_jobs "
            "WHERE status = 'pending' "
            "ORDER BY published_date ASC "
            "FOR UPDATE SKIP LOCKED "
            "LIMIT 1"
        )
        row = cur.fetchone()
        if row is None:
            if commit:
                conn.commit()
            return None

        mp3_url, meeting_title, source_url, published_date = row
        claimed_at = datetime.now(timezone.utc)
        cur.execute(
            "UPDATE granicus_transcription_jobs SET status = 'claimed', claimed_at = %s "
            "WHERE mp3_url = %s",
            (claimed_at, mp3_url),
        )

    if commit:
        conn.commit()

    return ClaimedJob(
        mp3_url=mp3_url,
        meeting_title=meeting_title,
        source_url=source_url,
        published_date=published_date,
    )


# --- Stale-row recovery ------------------------------------------------------


def recover_stale_claims(
    conn: psycopg.Connection,
    *,
    threshold: timedelta = STALE_CLAIM_THRESHOLD,
    now: datetime | None = None,
    commit: bool = True,
) -> list[str]:
    """Resets `status='claimed'` rows whose `claimed_at` is older than
    `threshold` back to `status='pending'` (clearing `claimed_at`) — a
    worker that claimed a row and then died (crashed, container killed)
    without ever reaching `_mark_completed`/`_mark_failed` leaves a row
    stuck in 'claimed' forever otherwise. See module docstring for the
    threshold's worked reasoning.

    `now` is injectable (defaults to real UTC now) for deterministic
    tests, matching `register.py`'s `today` injectable convention
    (DECISIONS #82). Returns the list of reclaimed `mp3_url`s (logged at
    WARNING — a reclaim means a worker died mid-job, worth noticing).
    """
    effective_now = now if now is not None else datetime.now(timezone.utc)
    cutoff = effective_now - threshold

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE granicus_transcription_jobs "
            "SET status = 'pending', claimed_at = NULL "
            "WHERE status = 'claimed' AND claimed_at < %s "
            "RETURNING mp3_url",
            (cutoff,),
        )
        reclaimed = [row[0] for row in cur.fetchall()]

    if reclaimed:
        logger.warning(
            "recovered %d stale claimed job(s) (claimed_at older than %s): %s",
            len(reclaimed),
            cutoff.isoformat(),
            reclaimed,
        )

    if commit:
        conn.commit()

    return reclaimed


# --- Audio download (Range-chunked, memory-bounded) --------------------------


def _parse_content_range_total(header: str) -> int:
    match = _CONTENT_RANGE_RE.match(header)
    if not match:
        raise CrawlerStructureError(f"unrecognized Content-Range header shape: {header!r}")
    return int(match.group(1))


def _fetch_chunk_with_retry(
    crawler: BaseCrawler,
    mp3_url: str,
    headers: dict[str, str],
    *,
    max_attempts: int,
    base_backoff_seconds: float,
) -> requests.Response:
    """One Range chunk fetch, retried with exponential backoff for
    transient network failures — mirrors `_transcribe_audio`'s retry
    shape. A non-transient failure (client error, scope/robots
    violation, anything `_is_retryable_download_error` doesn't
    recognize) raises immediately, no retry."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return crawler.fetch(mp3_url, headers=headers)
        except Exception as exc:
            if not _is_retryable_download_error(exc):
                raise
            if attempt >= max_attempts:
                logger.error(
                    "chunk fetch for %s failed after %d attempts (%s): %s",
                    mp3_url,
                    attempt,
                    type(exc).__name__,
                    exc,
                )
                raise
            delay = base_backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                "chunk fetch for %s attempt %d/%d failed (%s), retrying in %.1fs",
                mp3_url,
                attempt,
                max_attempts,
                type(exc).__name__,
                delay,
            )
            time.sleep(delay)


def _download_ranged(
    crawler: BaseCrawler,
    mp3_url: str,
    dest_path: Path,
    *,
    chunk_size: int = RANGE_CHUNK_SIZE_BYTES,
    max_bytes: int = WHISPER_MAX_FILE_BYTES,
    max_attempts: int = 3,
    base_backoff_seconds: float = 1.0,
) -> int:
    """Downloads `mp3_url` in Range-chunked windows straight to `dest_path`
    on disk — never accumulates the whole (possibly multi-GB) file in
    memory. Uses `BaseCrawler.fetch()` for every request (scope
    enforcement, robots.txt, rate-limiting, all still apply); overrides
    the User-Agent/Referer per-request for this CDN's documented
    bot-filter (see GRANICUS_CDN_USER_AGENT above) without changing the
    crawler's own honest identification used for its robots.txt check.
    Each chunk fetch is individually retried with backoff on a transient
    network failure (`_fetch_chunk_with_retry` /
    `_is_retryable_download_error`) — a single connection blip on one of
    several chunk requests must not permanently fail a hand-registered
    job that has no cheap re-registration path (DECISIONS #80).

    Reads the real total file size off the *first* response's
    `Content-Range` header and raises `AudioTooLargeError` immediately —
    before fetching any further chunks — if that total exceeds
    `max_bytes`. This is a deliberate design choice beyond what was
    strictly asked ("pre-flight check before calling the [Whisper] API"):
    since a real multi-hour meeting is virtually certain to be
    oversized, this bounds the wasted work to one ~8 MiB probe request
    instead of downloading a multi-GB file that's already known to be
    doomed to fail the size check. See module docstring's stale-claim
    threshold math, which relies on this early-abort behavior.

    Raises `CrawlerStructureError` (fail loud, never silently truncate)
    if a ranged response isn't actually 206, is missing `Content-Range`,
    reports a different total mid-download than the first chunk did, or
    the CDN reports more remaining data than it actually sends (an empty
    body before `downloaded` has reached `total` — a documented `requests`
    edge case, not a "never happens": a 206 with a truncated/empty body
    must never be silently accepted as "download complete"). A final
    `downloaded == total` check before returning is a second,
    belt-and-suspenders guard against the same failure mode, independent
    of the loop's own bookkeeping, so a truncated file can never reach
    `_transcribe_audio` even if a future change to this loop's logic
    introduces a way to exit it early.
    """
    total: int | None = None
    downloaded = 0

    with open(dest_path, "wb") as f:
        while total is None or downloaded < total:
            start = downloaded
            end = start + chunk_size - 1
            resp = _fetch_chunk_with_retry(
                crawler,
                mp3_url,
                {
                    "Range": f"bytes={start}-{end}",
                    "User-Agent": GRANICUS_CDN_USER_AGENT,
                    "Referer": GRANICUS_CDN_REFERER,
                },
                max_attempts=max_attempts,
                base_backoff_seconds=base_backoff_seconds,
            )

            if resp.status_code != 206:
                raise CrawlerStructureError(
                    f"expected 206 Partial Content from {mp3_url} (Range "
                    f"bytes={start}-{end}), got {resp.status_code} — the CDN's "
                    "Range-request support may have changed"
                )

            content_range = resp.headers.get("Content-Range")
            if not content_range:
                raise CrawlerStructureError(
                    f"206 response from {mp3_url} had no Content-Range header — "
                    "cannot determine total file size"
                )
            chunk_total = _parse_content_range_total(content_range)

            if total is None:
                total = chunk_total
                if total > max_bytes:
                    raise AudioTooLargeError(
                        f"{mp3_url} is {total:,} bytes, over whisper-1's "
                        f"{max_bytes:,}-byte single-request limit — aborted "
                        f"after the first {chunk_size:,}-byte probe chunk "
                        "rather than downloading the full file "
                        "(no multi-part transcription built this round, see "
                        "app/granicus/worker.py module docstring)"
                    )
            elif chunk_total != total:
                raise CrawlerStructureError(
                    f"Content-Range total changed mid-download for {mp3_url} "
                    f"({total} -> {chunk_total}) — source file may have "
                    "changed underneath this download"
                )

            body = resp.content
            if not body:
                raise CrawlerStructureError(
                    f"206 response from {mp3_url} returned an empty body "
                    f"after {downloaded:,}/{total:,} bytes had been received "
                    "— the CDN reported more data than it actually sent; "
                    "never silently treat this as a complete download"
                )
            f.write(body)
            downloaded += len(body)

    if downloaded != total:
        raise CrawlerStructureError(
            f"downloaded {downloaded:,} bytes from {mp3_url} but "
            f"Content-Range reported a total of {total:,} — a partial/"
            "truncated download is not safe to hand to Whisper"
        )

    return downloaded


# --- Transcription (OpenAI whisper-1) ---------------------------------------


def _transcribe_audio(
    openai_client: openai.OpenAI,
    audio_path: Path,
    *,
    model: str = WHISPER_MODEL,
    max_attempts: int = 3,
    base_backoff_seconds: float = 1.0,
) -> str:
    """Transcribes `audio_path` via whisper-1. Raises (never silently
    returns something malformed):
      - AudioTooLargeError before ever calling the API, if the file on
        disk exceeds WHISPER_MAX_FILE_BYTES (belt-and-suspenders — the
        download step already aborts earlier, but this stands alone if
        called with an already-downloaded file from another path).
      - The original openai exception, after exhausting max_attempts, for
        retryable failures (rate limit / transient network / 5xx).
      - The original openai exception immediately (no retry) for anything
        else (bad request — e.g. unsupported format, auth, etc).
      - TranscriptionValidationError if the API returns 200 but the
        payload doesn't have the expected shape.
    """
    size = audio_path.stat().st_size
    if size > WHISPER_MAX_FILE_BYTES:
        raise AudioTooLargeError(
            f"{audio_path} is {size:,} bytes on disk, over whisper-1's "
            f"{WHISPER_MAX_FILE_BYTES:,}-byte single-request limit"
        )

    attempt = 0
    while True:
        attempt += 1
        try:
            with open(audio_path, "rb") as f:
                response = openai_client.audio.transcriptions.create(model=model, file=f)
            break
        except _TRANSCRIBE_RETRYABLE_EXCEPTIONS as exc:
            if attempt >= max_attempts:
                logger.error(
                    "transcription call failed after %d attempts (retryable: %s): %s",
                    attempt,
                    type(exc).__name__,
                    exc,
                )
                raise
            delay = base_backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                "transcription call attempt %d/%d failed (%s), retrying in %.1fs",
                attempt,
                max_attempts,
                type(exc).__name__,
                delay,
            )
            time.sleep(delay)
        except openai.OpenAIError as exc:
            logger.error(
                "transcription call failed, not retrying (%s): %s", type(exc).__name__, exc
            )
            raise

    return _validate_transcription(response)


def _validate_transcription(response) -> str:
    """Model output is untrusted input (.claude/rules/rag.md) — checked
    before this ever reaches `transcript_text`."""
    text = getattr(response, "text", None)
    if not isinstance(text, str) or not text.strip():
        raise TranscriptionValidationError(
            f"expected a non-empty .text string on the transcription response, got {text!r}"
        )
    return text


# --- Storing the result -------------------------------------------------------


def _mark_completed(conn: psycopg.Connection, mp3_url: str, transcript_text: str, *, commit: bool = True) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE granicus_transcription_jobs "
            "SET status = 'completed', transcript_text = %s "
            "WHERE mp3_url = %s",
            (transcript_text, mp3_url),
        )
    if commit:
        conn.commit()


def _mark_failed(conn: psycopg.Connection, mp3_url: str, failure_reason: str, *, commit: bool = True) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE granicus_transcription_jobs "
            "SET status = 'failed', failure_reason = %s "
            "WHERE mp3_url = %s",
            (failure_reason, mp3_url),
        )
    if commit:
        conn.commit()


def _revert_to_pending(conn: psycopg.Connection, mp3_url: str, *, commit: bool = True) -> None:
    """Reverts a claimed row back to 'pending' (clears `claimed_at`)
    WITHOUT recording a failure_reason. Used specifically for a systemic
    failure (e.g. `openai.AuthenticationError` — a broken API key, not a
    problem with this particular meeting's audio) — see
    `process_one_job`'s systemic-vs-per-job exception handling below,
    mirroring DECISIONS #74's shape for `embed_and_insert_chunks()`.

    Deliberately NOT `_mark_failed`: this job did nothing wrong and has
    no cheap re-registration path (DECISIONS #80 — a human re-resolves
    the MP3 URL by hand), so permanently marking it 'failed' for a
    credential problem would misattribute the failure to the job and
    require a manual DB fix to retry it once the real (systemic) problem
    is resolved. Reverting to 'pending' means the very next worker run,
    after the credential is fixed, picks this job back up for free.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE granicus_transcription_jobs SET status = 'pending', claimed_at = NULL "
            "WHERE mp3_url = %s",
            (mp3_url,),
        )
    if commit:
        conn.commit()


# --- Tying it together --------------------------------------------------------


def process_one_job(
    conn: psycopg.Connection,
    openai_client: openai.OpenAI,
    crawler: BaseCrawler,
    *,
    tmp_dir: Path | None = None,
) -> ProcessResult | None:
    """Claims one pending job, downloads its audio, transcribes it, and
    stores the result — end to end. Returns None if nothing was pending
    (a clean, expected no-op, not an error). Returns a ProcessResult with
    a terminal status ('completed' or 'failed') for every ordinary
    per-job outcome. The one exception: a systemic OpenAI auth/permission
    failure (`openai.AuthenticationError`/`PermissionDeniedError`) is NOT
    turned into a ProcessResult — the job is reverted to 'pending' (see
    `_revert_to_pending`) and the exception is re-raised, so a caller
    looping over multiple jobs (`process_pending_jobs`) aborts instead of
    claiming and permanently failing every remaining job against the
    same broken credential. Outside that one case,
    `granicus_transcription_jobs` never ends this call sitting in
    'claimed' for a job this function actually picked up, except in the
    genuinely-unexpected case of the process dying mid-call, which is
    exactly what `recover_stale_claims` exists to clean up later.

    Best-effort local temp-file cleanup on every exit path
    (.claude/rules/data.md's "best-effort cleanup on partial failure",
    applied here to the downloaded audio file rather than a DB row).
    """
    job = claim_next_job(conn)
    if job is None:
        return None

    tmp_dir = tmp_dir if tmp_dir is not None else Path(tempfile.gettempdir())
    audio_path = tmp_dir / f"granicus-{uuid4().hex}.mp3"

    try:
        try:
            _download_ranged(crawler, job.mp3_url, audio_path)
        except AudioTooLargeError as exc:
            reason = str(exc)
            _mark_failed(conn, job.mp3_url, reason)
            return ProcessResult(mp3_url=job.mp3_url, status="failed", failure_reason=reason)
        except Exception as exc:  # noqa: BLE001 - fail loud with a specific reason, never swallow
            reason = f"audio download failed ({type(exc).__name__}): {exc}"
            _mark_failed(conn, job.mp3_url, reason)
            return ProcessResult(mp3_url=job.mp3_url, status="failed", failure_reason=reason)

        try:
            transcript_text = _transcribe_audio(openai_client, audio_path)
        except AudioTooLargeError as exc:
            reason = str(exc)
            _mark_failed(conn, job.mp3_url, reason)
            return ProcessResult(mp3_url=job.mp3_url, status="failed", failure_reason=reason)
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            # Systemic, not a per-job problem (DECISIONS #74's shape,
            # applied here) — every remaining pending job would fail
            # identically against the same broken credential, so this
            # job is reverted to 'pending' (not permanently 'failed',
            # see _revert_to_pending's docstring) and the exception is
            # re-raised so process_pending_jobs aborts the whole batch
            # instead of burning through every remaining job one at a
            # time with the same doomed API call.
            _revert_to_pending(conn, job.mp3_url)
            raise
        except Exception as exc:  # noqa: BLE001 - fail loud with a specific reason, never swallow
            reason = f"transcription failed ({type(exc).__name__}): {exc}"
            _mark_failed(conn, job.mp3_url, reason)
            return ProcessResult(mp3_url=job.mp3_url, status="failed", failure_reason=reason)

        _mark_completed(conn, job.mp3_url, transcript_text)
        return ProcessResult(mp3_url=job.mp3_url, status="completed")
    finally:
        audio_path.unlink(missing_ok=True)


def process_pending_jobs(
    conn: psycopg.Connection,
    openai_client: openai.OpenAI,
    crawler: BaseCrawler,
    *,
    max_jobs: int = 1,
    tmp_dir: Path | None = None,
) -> list[ProcessResult]:
    """Thin loop over `process_one_job`, stopping after `max_jobs` jobs or
    once the pending queue is empty, whichever comes first.

    `max_jobs` has NO unbounded default and defaults to 1 deliberately —
    this is explicitly NOT the full 12-month backfill runner
    (`.claude/rules/crawler.md`: validate a small batch first; that
    unattended full-backfill runner is out of scope this round per the
    orchestrator's brief). A caller doing a real validation batch must
    say so explicitly by passing a larger `max_jobs`.

    Aborts the whole batch (raises, does not swallow) on a systemic
    OpenAI auth/permission failure rather than continuing to claim and
    permanently fail every remaining pending job against the same broken
    credential — see `process_one_job`'s docstring and
    `_revert_to_pending`. Every other per-job failure (a bad audio file,
    an oversized file, a transient network blip that exhausted its
    retries) is caught inside `process_one_job` itself and does NOT abort
    the batch — only this one systemic case does.
    """
    results: list[ProcessResult] = []
    for _ in range(max_jobs):
        try:
            result = process_one_job(conn, openai_client, crawler, tmp_dir=tmp_dir)
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            logger.error(
                "aborting batch after %d completed job(s): systemic OpenAI "
                "auth/permission failure, not a per-job problem (DECISIONS "
                "#74's shape) — fix credentials before retrying",
                len(results),
            )
            raise
        if result is None:
            break
        results.append(result)
    return results
