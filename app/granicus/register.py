"""Human-supplied meeting registration for the Granicus transcription
pipeline (DECISIONS #79-82, superseding #12's automated-discovery clause
only — #12's async-worker pattern, hosted transcription API, and 12-month
backfill bound all still stand).

Automated *discovery* of St. Petersburg City Council meetings (RSS feed,
MediaPlayer.php resolution) is out of scope: stpete.granicus.com's
robots.txt blocks this project's honest User-Agent from the entire host
(DECISIONS #77), and that host has been removed from
app.crawlers.base.ALLOWED_SOURCE_HOSTS (DECISIONS #79) — no code in this
module (or anywhere else in this codebase) ever fetches it. A human
(Amber, via a real manual browser session — never an automated fetch)
finds the meeting on stpete.granicus.com herself, resolves its
MediaPlayer.php page down to the real archive-video.granicus.com direct
MP3 URL herself, and supplies both URLs to register_meeting() below. The
MediaPlayer.php URL is recorded as this job's citable source_url for
attribution — recording a URL string is not the same as fetching it.

Automated *fetch-and-transcribe*, given an already-known direct MP3 URL,
stays fully automated and is explicitly OUT of scope for THIS module —
that's the Day 5+ polling worker (not built yet), which will claim rows
this module inserts with status='pending'.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from urllib.parse import urlparse

import psycopg

from app.crawlers.base import Attribution
from app.granicus.schema import GRANICUS_JOB_COLUMNS

# DECISIONS #12's hard 12-month backfill bound, still binding — enforced
# HERE, at registration time (DECISIONS #82), not deferred to the Day 5+
# worker. Registration is now the only entry point a meeting can enter
# the jobs table through, so it's the correct place left to enforce this
# in code.
BACKFILL_MONTHS = 12

# The two hosts a registered meeting's URLs must belong to. Deliberately
# NOT app.crawlers.base.ALLOWED_SOURCE_HOSTS reused directly — that
# allow-list governs what BaseCrawler.fetch() may ever REQUEST, and
# nothing here ever calls fetch(). This is a narrower, registration-time
# sanity check that a human didn't paste the wrong URL into the wrong
# field (e.g. swapped the two), independent of and no substitute for
# DECISIONS #17's fetch-time enforcement.
EXPECTED_MP3_HOST = "archive-video.granicus.com"
EXPECTED_MEDIAPLAYER_HOST = "stpete.granicus.com"


class BackfillBoundError(Exception):
    """Raised when meeting_date is older than DECISIONS #12's 12-month
    backfill bound. Registration-time rejection, not a worker-time one —
    an out-of-window meeting never becomes a 'pending' row at all."""


class RegistrationValidationError(Exception):
    """Raised when a supplied field is obviously wrong (empty title, a
    URL on the wrong host, a future-dated meeting) — catches a bad input
    before it becomes a 'pending' row a future worker would try to act
    on."""


@dataclass(frozen=True)
class RegistrationResult:
    mp3_url: str
    # False if this mp3_url was already registered — a safe, idempotent
    # no-op re-run, not an error. See DECISIONS #80 for why re-running the
    # same registration twice is intentionally a no-op rather than an
    # update.
    inserted: bool


def _earliest_allowed_meeting_date(today: date) -> date:
    """12 calendar months before `today`. stdlib `date` has no built-in
    month arithmetic and this project has no `dateutil` dependency
    elsewhere (nor is one worth adding for this single call site) — this
    walks the year/month back by hand and clamps a day that doesn't exist
    in the target month (e.g. today=Aug 31 -> no Feb 31) down to the
    latest real day in that month, the same "clamp, don't crash or
    silently roll into the wrong month" behavior dateutil's
    relativedelta would give."""
    year = today.year
    month = today.month - BACKFILL_MONTHS
    while month <= 0:
        month += 12
        year -= 1
    day = today.day
    while True:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1


def _validate_host(url: str, expected_host: str, field_name: str) -> None:
    host = urlparse(url).hostname
    if host != expected_host:
        raise RegistrationValidationError(
            f"{field_name}={url!r} must be a {expected_host} URL, got host {host!r}"
        )


def register_meeting(
    conn: psycopg.Connection,
    *,
    meeting_date: date,
    meeting_title: str,
    mp3_url: str,
    mediaplayer_url: str,
    today: date | None = None,
    commit: bool = True,
) -> RegistrationResult:
    """Validates and inserts one meeting as a 'pending' transcription job.

    Never fetches either URL — mp3_url and mediaplayer_url are trusted
    strings supplied by a human who resolved them via a real manual
    browser session (see module docstring). Raises BackfillBoundError or
    RegistrationValidationError BEFORE any database write on a bad input
    — never inserts a partially-invalid row.

    `today` is injectable (defaults to date.today()) so the 12-month
    bound can be tested deterministically without depending on the real
    calendar date (DECISIONS #82).
    """
    if not meeting_title or not meeting_title.strip():
        raise RegistrationValidationError("meeting_title must not be empty")

    _validate_host(mp3_url, EXPECTED_MP3_HOST, "mp3_url")
    _validate_host(mediaplayer_url, EXPECTED_MEDIAPLAYER_HOST, "mediaplayer_url")

    effective_today = today if today is not None else date.today()

    if meeting_date > effective_today:
        raise RegistrationValidationError(
            f"meeting_date {meeting_date.isoformat()} is in the future "
            f"(today is {effective_today.isoformat()})"
        )

    earliest_allowed = _earliest_allowed_meeting_date(effective_today)
    if meeting_date < earliest_allowed:
        raise BackfillBoundError(
            f"meeting_date {meeting_date.isoformat()} is older than the 12-month "
            f"backfill bound (DECISIONS #12) — earliest allowed as of "
            f"{effective_today.isoformat()} is {earliest_allowed.isoformat()}"
        )

    attribution = Attribution.now(source_url=mediaplayer_url, published_date=meeting_date)

    columns_sql = ", ".join(GRANICUS_JOB_COLUMNS)
    placeholders = ", ".join(["%s"] * len(GRANICUS_JOB_COLUMNS))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO granicus_transcription_jobs ({columns_sql}) "
            f"VALUES ({placeholders}) "
            "ON CONFLICT (mp3_url) DO NOTHING",
            (
                mp3_url,
                meeting_title.strip(),
                attribution.source_url,
                attribution.published_date,
                attribution.retrieval_timestamp,
                "pending",
                None,  # claimed_at
                None,  # transcript_text
                None,  # failure_reason
            ),
        )
        inserted = cur.rowcount == 1

    if commit:
        conn.commit()

    return RegistrationResult(mp3_url=mp3_url, inserted=inserted)
