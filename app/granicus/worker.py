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


## Stale-claim recovery threshold: worked reasoning (re-derived — see
## "decision (c)" below for why the design this math depends on changed)

`STALE_CLAIM_THRESHOLD = timedelta(hours=4)`. This supersedes the
original `timedelta(hours=1)` derivation, which explicitly depended on a
design that no longer exists: the original math's dominant assumption was
"the common case fails loud after one bounded ~8 MiB probe fetch... never
reaches Whisper at all," because `_download_ranged` used to abort the
instant a file was found to exceed `WHISPER_MAX_FILE_BYTES` (25 MiB).
Now that oversized files are downloaded in full and split (decision (c)
below), that assumption is false for the realistic common case — a real
multi-hour meeting now downloads completely AND makes multiple whisper-1
calls, not one. Re-derived from scratch, same worked-math rigor:

**A'. Realistic worst case, grounded in DECISIONS #116's real evidence.**
Using the exact real byte count of the largest real file seen to date
(229,683,110 bytes — the Aug 13 2026 City Council session, the largest of
the 3 real meetings DECISIONS #116 registered), not a rounded "~250 MB"
placeholder (crawler-review pre-commit finding, LOW — the original
version of this section rounded up to "250 MB" and then mixed decimal-MB
and MiB readings between the download-chunk and piece-count steps; using
one real, exact byte count throughout removes that ambiguity), at
DECISIONS #116's measured ~137.7 kbps bitrate:
  - Real duration at this bitrate: 229,683,110 × 8 / 137,700 ≈ 13,343
    sec ≈ **222 minutes (≈3.7 hours)**.
  - Download: `RANGE_CHUNK_SIZE_BYTES` = 8,388,608 bytes ⇒ ceil(229,683,110
    / 8,388,608) = 28 ranged requests. Real transfer time dominates over
    the rate-limit floor at this chunk size (8 MiB at a conservative 5
    Mbps ≈ 13s/chunk); generously bounding each chunk (transfer +
    rate-limit wait + margin) at 30s, matching the original docstring's
    own pessimistic per-chunk bound ⇒ 28 × 30s ≈ **14 minutes**.
  - Split (local `pydub`/`ffmpeg` decode + re-export, `app/granicus/
    audio_split.py`): CPU-bound, no network wait — generously bounded at
    ≤ the download time itself (14 min). This bound is inherited/asserted
    reasoning (local audio-codec throughput is essentially always faster
    than an 8 MiB/30s-chunk-bounded network transfer of the same bytes,
    ≈2.1 Mbps-equivalent sustained), not independently re-benchmarked
    against a wall-clock measurement in this environment before this
    threshold was set (crawler-review pre-commit finding, MEDIUM — flagged
    honestly rather than presented as measured).
  - Transcribe: `SPLIT_TARGET_FRACTION = 0.9` against `WHISPER_MAX_FILE_BYTES`
    (26,214,400 bytes) gives a 23,592,960-byte per-piece target ⇒
    ceil(229,683,110 / 23,592,960) = **10 pieces**. Realistic per-piece
    cost uses the ORIGINAL docstring's own single-attempt bound (not full
    3-attempt retry exhaustion for every piece — see the pessimistic
    cross-check below for that), itself an inherited, not re-benchmarked,
    assumption: each attempt's wall-clock is bounded generously at 5
    minutes ⇒ 10 × 5 min = **50 minutes**.
  - Total: 14 + 14 + 50 = **78 minutes** realistic worst case for the
    largest real meeting seen to date.

**B'. Pessimistic cross-check — every one of those 10 pieces
independently exhausts all 3 retry attempts** (the original docstring's
own full 15-min-per-call bound, applied per piece — a combinatorially
unlikely scenario, since it requires 10 independent transient failures in
the same job, but worth bounding explicitly rather than assumed away):
10 × 15 min = 150 min, plus the same ≈28 min download+split ⇒ **≈178
minutes (≈3.0 hours)**.

**C'. Sanity check against the outer boundary this design still permits**
(`MAX_TOTAL_DOWNLOAD_BYTES` = 524,288,000 bytes, see decision (c) below) —
a never-yet-observed 500 MiB file (≈8.46 hr at the real 137.7 kbps rate)
would need ceil(524,288,000/23,592,960) = 23 pieces: download
ceil(524,288,000/8,388,608) = 63 chunks × 30s ≈ 32 min, split ≤32 min
(same bound), transcribe at realistic single-attempt timing 23 × 5 min ≈
115 min ⇒ **≈179 minutes (≈3.0 hours)** — still under 4 hours even at
this extreme, never-observed boundary, at realistic (non-retry-exhausted)
per-piece timing.

Setting the threshold at **4 hours (240 minutes)**: ≈3.1x margin over the
realistic A' estimate (78 min); ≈35% margin over both B''s
combinatorial-pessimism cross-check and C''s outer-boundary case (178-179
min) — real margin, not huge, and honestly reported as such rather than
oversold (crawler-review pre-commit finding, MEDIUM: an earlier version of
this section claimed B' was "comfortably" cleared when the underlying
numbers, before this exact-byte-count correction, gave only ~13% margin).
This constant's actual job is unchanged from the original derivation —
reclaiming a genuinely DEAD worker's claim, not bounding the maximum time
a legitimately-still-working job could ever take. A finite threshold
always accepts some tradeoff between "too short: reclaims and
double-processes a job that's still honestly in flight" and "too long: a
dead worker's claim blocks that row longer before recovery" — 4 hours
stays on the safe side of that tradeoff for every real and near-worst-case
scenario derived above, while still being far short of a full day for the
(small, manually registered) queue this project runs. If the split/
transcribe timing bounds above are ever found to be wrong once measured
for real (rather than asserted), this threshold should be re-derived
against real numbers, not defended past new evidence.


## Range-chunk size: 8 MiB

Large enough to keep the number of HTTP round-trips (each paying the
2.0s/host rate-limit floor) reasonable — a ≤25 MiB file needs only ~4
requests. Small enough to keep peak resident memory for one in-flight
chunk trivial regardless of total file size, and comfortably under the
25 MiB Whisper ceiling itself, so a single chunk can never by itself
exceed that limit. `RANGE_CHUNK_SIZE_BYTES = 8 * 1024 * 1024`.


## >25 MiB Whisper case: decision (c) — duration-aware chunked
## transcription, superseding decision (b)

**History, kept rather than deleted, because it's interview-defensible
context for why this changed.** DECISIONS #86 originally built (b): fail
loud with a specific `failure_reason` (`AudioTooLargeError`, raised the
moment the first Range chunk's `Content-Range` total was known to exceed
`WHISPER_MAX_FILE_BYTES`), not split-and-concatenate — specifically
because (i) real audio-duration-aware splitting needed a new dependency
(pydub, wrapping ffmpeg) with real Docker/deploy implications not decided
that round, and (ii) a raw byte-offset split of an MP3 stream isn't
frame-safe (MP3 frames aren't fixed-length; a naive cut can corrupt both
halves). DECISIONS #116 then proved with real data that this wasn't a
rare edge case: all 3 real St. Petersburg City Council meetings tried
(150-230 MB, ~137.7 kbps measured) failed identically under (b) — full
sessions run 1.5-5+ hours, vastly over the ~25-minute ceiling this limit
implied at real bitrates. Amber explicitly authorized building the
follow-up (b) had flagged but not built.

**What changed:** (i) is resolved — `pydub` is now a decided, pinned
dependency (`requirements.txt`), and `ffmpeg` is added to the Docker
image (deploy-infra, parallel work this round). (ii) is resolved by
construction, not worked around: `app/granicus/audio_split.py` never
touches the compressed byte stream directly — it decodes via
`pydub.AudioSegment.from_file` (wrapping `ffmpeg`) and slices in the
*decoded* PCM domain, where cut points are sample-accurate by
definition, then re-exports each slice as its own independent, valid
MP3. The frame-safety objection that blocked (b)'s split option never
applied to decoded-domain slicing — it only ever applied to a raw
byte-offset cut of the still-compressed file, which this module never
does. See `audio_split.py`'s own module docstring for the split-sizing
strategy (initial real-bitrate estimate + per-piece post-export
byte-size verification with adaptive re-split) in full.

**Verified against the current OpenAI docs** (developers.openai.com/api/docs/guides/speech-to-text,
fetched DECISIONS #86's session — platform.openai.com/docs/... 301-redirects
there now, unchanged since): whisper-1's real limit is "files can be up
to 25 MB"; supported formats are mp3/mp4/mpeg/mpga/m4a/wav/webm.
`transcriptions.create()`'s `chunking_strategy` parameter remains
*server-side* VAD chunking within one request/25 MiB, explicitly
documented as ignored for `whisper-1` — still no SDK-provided
multi-request chunking helper, confirming this module's manual
per-piece-call approach (loop calling the existing `_transcribe_audio()`
once per split piece, `_transcribe_possibly_split` below) is still the
only real option.

`WHISPER_MAX_FILE_BYTES = 25 * 1024 * 1024` (26,214,400 bytes — the MiB
reading of "25 MB", the commonly-reported actually-enforced byte ceiling;
I could not verify the exact byte-for-byte enforcement point against a
live API call in this environment, so this is the conservative
interpretation, not a confirmed-exact one). This is now the per-piece
ceiling (unchanged in value), not the whole-download abort ceiling — see
`MAX_TOTAL_DOWNLOAD_BYTES` below for the new, larger, explicit outer
sanity bound on what this worker will ever attempt to download and split
in one job.

**`MAX_TOTAL_DOWNLOAD_BYTES = 500 * 1024 * 1024` (500 MiB =
524,288,000 bytes) — explicit outer ceiling, still fail loud above it.**
The brief for this round explicitly asked whether there's a sane upper
bound above which this worker should still fail loud rather than attempt
an enormous number of whisper-1 calls. DECISIONS #116's real evidence
caps out at 229,683,110 bytes / ~3.7 hours for the largest actual St.
Pete City Council session seen to date (exact real byte count, not a
rounded estimate — see the stale-claim threshold's re-derivation above
for the same figure used consistently) — 524,288,000 bytes is ~2.3x that
real observed maximum (~8.46 hours of audio at the real 137.7 kbps rate),
comfortably covering any real meeting this project has ever actually seen
with margin, while still catching a genuine outlier
(a mis-registered non-audio file, a wrong URL, an accidentally
multi-day recording) before this worker would attempt an unbounded
number of Whisper calls against it. `_download_ranged` keeps its
existing early-abort behavior (probe the first Range chunk's
`Content-Range` total, abort immediately if it exceeds this ceiling,
never download further) — unchanged mechanism, just a larger, explicitly
documented threshold. `AudioTooLargeError` is still the exception raised
for this case; its meaning shifted from "over Whisper's own limit" (no
longer directly downloader-enforced — that's now `audio_split.py`'s job)
to "over this project's own sane processing ceiling."

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
from app.granicus.audio_split import AudioSplitResult, split_audio_for_whisper

logger = logging.getLogger("granicus.worker")

# --- Tunable constants (see module docstring for the worked reasoning) ----

STALE_CLAIM_THRESHOLD = timedelta(hours=4)
RANGE_CHUNK_SIZE_BYTES = 8 * 1024 * 1024  # 8 MiB
WHISPER_MODEL = "whisper-1"
WHISPER_MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MiB ("25 MB" per OpenAI docs) — the
# per-piece ceiling `audio_split.py` splits against, not the whole-download ceiling.
MAX_TOTAL_DOWNLOAD_BYTES = 500 * 1024 * 1024  # 500 MiB — outer sanity ceiling, see
# module docstring's "decision (c)" section for the worked reasoning.
WHISPER_COST_PER_MINUTE_USD = 0.006  # verified live, module docstring above.

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
    """Raised when a downloaded audio file exceeds this project's outer
    sanity ceiling (`MAX_TOTAL_DOWNLOAD_BYTES`) — fails loud instead of
    attempting to download/split/transcribe a genuine outlier. See module
    docstring, "decision (c)". (Prior to decision (c), this was also
    raised for anything over whisper-1's own 25 MiB per-request limit;
    that case is now handled by splitting, not failing — see
    `_transcribe_possibly_split` / `app/granicus/audio_split.py`.)"""


class PieceTranscriptionError(Exception):
    """Raised when one split piece's transcription ultimately fails (after
    `_transcribe_audio`'s own retry/classification is exhausted) — names
    which piece and why, so a stitched-multi-piece job's failure_reason
    never silently drops which segment broke
    (`.claude/rules/crawler.md`'s fail-loud rule, applied to a partial-job
    failure exactly as much as a whole-job one)."""


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
    max_bytes: int = MAX_TOTAL_DOWNLOAD_BYTES,
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
    `max_bytes` (defaults to `MAX_TOTAL_DOWNLOAD_BYTES`, the project's
    outer sanity ceiling — see module docstring's "decision (c)", NOT
    `WHISPER_MAX_FILE_BYTES`; a file over Whisper's own per-request limit
    but under this outer ceiling is downloaded in full and split
    afterward by `app/granicus/audio_split.py`, not aborted here). This
    bounds the wasted work on a genuine outlier (a mis-registered
    non-audio file, a wrong URL) to one probe request instead of
    downloading a multi-GB file that's already known to be doomed.

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
                        f"{mp3_url} is {total:,} bytes, over this project's "
                        f"{max_bytes:,}-byte outer sanity ceiling — aborted "
                        f"after the first {chunk_size:,}-byte probe chunk "
                        "rather than downloading the full file (see "
                        "MAX_TOTAL_DOWNLOAD_BYTES, app/granicus/worker.py "
                        "module docstring's 'decision (c)' section)"
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


# --- Transcription, split-aware (decision (c), see module docstring) --------

# Stitch separator between split-piece transcript texts — a plain space,
# matching app/chunking/granicus_transcript.py's own `_PIECE_JOIN`
# convention for joining sentence groups back into chunk text. Confirmed
# compatible with that module before choosing this: its chunker only ever
# splits `transcript_text` at sentence-boundary punctuation
# (`_SENTENCE_SPLIT`), never assumes anything about piece boundaries, and
# already has a documented, accepted "LOW" precedent for a chunk boundary
# landing exactly at a piece-adjacent seam (its own "St. Petersburg"
# mid-name split note) — a plain space join introduces nothing new for
# that downstream chunker to handle.
_PIECE_STITCH_SEPARATOR = " "


def _transcribe_possibly_split(
    openai_client: openai.OpenAI,
    audio_path: Path,
    job: ClaimedJob,
    tmp_dir: Path,
) -> str:
    """Transcribes `audio_path`, splitting first if it exceeds
    `WHISPER_MAX_FILE_BYTES` (decision (c) — see module docstring).

    The single-piece path (the common case for most real meetings to
    date, DECISIONS #116) is completely unchanged: one `_transcribe_audio`
    call, no split module invoked at all.

    For an oversized file: splits via `audio_split.split_audio_for_whisper`,
    logs the real estimated cost (`.claude/rules/crawler.md`: multiple
    whisper-1 calls for one job is materially bigger spend than the
    single-call case this worker was originally built for) at WARNING
    before making any of the per-piece calls, then calls the existing
    `_transcribe_audio()` once per piece — preserving its existing
    retry/failure-classification shape unchanged.

    A systemic OpenAI auth/permission failure
    (`openai.AuthenticationError`/`PermissionDeniedError`) on ANY piece
    propagates immediately, unwrapped — `process_one_job`'s existing
    systemic-vs-per-job handling (below) already special-cases this exact
    exception type for the whole job, and that's the correct behavior
    here too: a broken credential isn't specific to one piece or one job.

    Any OTHER piece failure is wrapped in `PieceTranscriptionError` naming
    which piece (`N/total`) failed and why, then raised — the whole job
    fails loud (never a stitched result silently missing one segment's
    text), landing in `process_one_job`'s existing generic per-job
    `except Exception` handler with a `failure_reason` that already names
    the failing piece.

    Split-piece temp files are best-effort cleaned up in a `finally`
    (`.claude/rules/data.md`) regardless of outcome — these are in
    addition to `process_one_job`'s own `audio_path` cleanup, which
    covers only the original downloaded file, not these derived pieces.
    """
    size = audio_path.stat().st_size
    if size <= WHISPER_MAX_FILE_BYTES:
        return _transcribe_audio(openai_client, audio_path)

    split_result: AudioSplitResult = split_audio_for_whisper(
        audio_path, tmp_dir, max_piece_bytes=WHISPER_MAX_FILE_BYTES
    )
    piece_count = len(split_result.pieces)
    estimated_cost_usd = (split_result.total_duration_seconds / 60) * WHISPER_COST_PER_MINUTE_USD
    logger.warning(
        "%r (%s): audio is %d bytes (~%.1f min), over whisper-1's %d-byte "
        "single-request limit — split into %d piece(s), about to make %d "
        "separate whisper-1 calls, estimated real cost ~$%.2f at $%.3f/min",
        job.meeting_title,
        job.mp3_url,
        size,
        split_result.total_duration_seconds / 60,
        WHISPER_MAX_FILE_BYTES,
        piece_count,
        piece_count,
        estimated_cost_usd,
        WHISPER_COST_PER_MINUTE_USD,
    )

    texts: list[str] = []
    try:
        for piece in split_result.pieces:
            try:
                piece_text = _transcribe_audio(openai_client, piece.path)
            except (openai.AuthenticationError, openai.PermissionDeniedError):
                raise
            except Exception as exc:  # noqa: BLE001 - re-wrapped with piece context, never swallowed
                raise PieceTranscriptionError(
                    f"piece {piece.index}/{piece_count} ({piece.path.name}) of "
                    f"{job.mp3_url!r} failed ({type(exc).__name__}): {exc}"
                ) from exc
            texts.append(piece_text)
    finally:
        for piece in split_result.pieces:
            piece.path.unlink(missing_ok=True)

    return _PIECE_STITCH_SEPARATOR.join(texts)


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
    """Claims one pending job, downloads its audio, transcribes it (via
    `_transcribe_possibly_split` — splitting first if the file exceeds
    whisper-1's 25 MiB per-request limit, decision (c) in the module
    docstring), and stores the result — end to end. Returns None if
    nothing was pending (a clean, expected no-op, not an error). Returns a
    ProcessResult with a terminal status ('completed' or 'failed') for
    every ordinary per-job outcome — including a `PieceTranscriptionError`
    from a single split piece's failure, which lands in the generic
    per-job `except Exception` branch below with a `failure_reason`
    naming which piece failed and why. The one exception: a systemic
    OpenAI auth/permission failure (`openai.AuthenticationError`/`PermissionDeniedError`) is NOT
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
            transcript_text = _transcribe_possibly_split(openai_client, audio_path, job, tmp_dir)
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
