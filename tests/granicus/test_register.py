"""Tests for app/granicus/register.py — DECISIONS #79-82.

Pure validation logic (host checks, empty-title, the 12-month backfill
bound math) needs no database and is tested standalone. Insert/idempotency
behavior runs against the real local Postgres (tests/granicus/conftest.py),
using commit=False throughout so the db_conn fixture's rollback-at-
teardown cleans up automatically — no test here needs its own DELETE.

No live network call is made or mocked anywhere in this file: nothing in
app/granicus/register.py ever fetches a URL (DECISIONS #79's whole point),
so there's nothing to mock — this is pure unit/schema testing, not
`responses`-mocked HTTP.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.granicus.register import (
    BACKFILL_MONTHS,
    BackfillBoundError,
    RegistrationValidationError,
    _earliest_allowed_meeting_date,
    register_meeting,
)

VALID_MP3_URL = "https://archive-video.granicus.com/stpete/abc123.mp3"
VALID_MEDIAPLAYER_URL = "https://stpete.granicus.com/MediaPlayer.php?view_id=1&clip_id=999"


# --- _earliest_allowed_meeting_date: pure date math, no DB -----------------


def test_earliest_allowed_meeting_date_basic():
    assert _earliest_allowed_meeting_date(date(2026, 8, 22)) == date(2025, 8, 22)


def test_earliest_allowed_meeting_date_crosses_year_boundary():
    assert _earliest_allowed_meeting_date(date(2026, 3, 1)) == date(2025, 3, 1)


def test_earliest_allowed_meeting_date_clamps_nonexistent_day():
    # These same-month/same-day cases don't actually hit the clamp branch
    # (every month but February has the same day-count every year, and
    # BACKFILL_MONTHS=12 always lands on the same month) — kept as sanity
    # checks that ordinary dates round-trip unchanged.
    assert _earliest_allowed_meeting_date(date(2026, 8, 31)) == date(2025, 8, 31)
    assert _earliest_allowed_meeting_date(date(2025, 3, 31)) == date(2024, 3, 31)
    assert _earliest_allowed_meeting_date(date(2024, 4, 30)) == date(2023, 4, 30)


def test_earliest_allowed_meeting_date_clamps_leap_day():
    # The ONLY day/month combination a fixed 12-month-back bound can ever
    # actually produce a nonexistent date for: Feb 29 (leap year) minus 12
    # months lands on Feb 29 of a non-leap year, which doesn't exist —
    # must clamp down to Feb 28, not raise or silently roll into March.
    assert _earliest_allowed_meeting_date(date(2024, 2, 29)) == date(2023, 2, 28)
    assert _earliest_allowed_meeting_date(date(2028, 2, 29)) == date(2027, 2, 28)


def test_backfill_months_is_twelve():
    # Pins the constant itself — DECISIONS #12's bound is "12 months", not
    # a number that should silently drift via an edit to the helper alone.
    assert BACKFILL_MONTHS == 12


# --- Validation, no DB needed (fails before any query) ----------------------


def test_register_meeting_rejects_meeting_older_than_12_months(db_conn):
    today = date(2026, 8, 22)
    too_old = date(2025, 8, 21)  # one day before the 12-month cutoff

    with pytest.raises(BackfillBoundError):
        register_meeting(
            db_conn,
            meeting_date=too_old,
            meeting_title="Ancient Meeting",
            mp3_url=VALID_MP3_URL,
            mediaplayer_url=VALID_MEDIAPLAYER_URL,
            today=today,
            commit=False,
        )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM granicus_transcription_jobs WHERE mp3_url = %s", (VALID_MP3_URL,)
        )
        assert cur.fetchone() is None


def test_register_meeting_accepts_meeting_exactly_at_the_boundary(db_conn):
    today = date(2026, 8, 22)
    exactly_boundary = date(2025, 8, 22)  # exactly 12 months back — allowed

    result = register_meeting(
        db_conn,
        meeting_date=exactly_boundary,
        meeting_title="Boundary Meeting",
        mp3_url=VALID_MP3_URL,
        mediaplayer_url=VALID_MEDIAPLAYER_URL,
        today=today,
        commit=False,
    )
    assert result.inserted is True


def test_register_meeting_rejects_future_dated_meeting(db_conn):
    today = date(2026, 8, 22)
    tomorrow = date(2026, 8, 23)

    with pytest.raises(RegistrationValidationError, match="future"):
        register_meeting(
            db_conn,
            meeting_date=tomorrow,
            meeting_title="Time Traveler Meeting",
            mp3_url=VALID_MP3_URL,
            mediaplayer_url=VALID_MEDIAPLAYER_URL,
            today=today,
            commit=False,
        )


def test_register_meeting_rejects_wrong_host_mp3_url(db_conn):
    with pytest.raises(RegistrationValidationError, match="mp3_url"):
        register_meeting(
            db_conn,
            meeting_date=date(2026, 8, 13),
            meeting_title="Wrong MP3 Host Meeting",
            mp3_url="https://evil.example.com/audio.mp3",
            mediaplayer_url=VALID_MEDIAPLAYER_URL,
            today=date(2026, 8, 22),
            commit=False,
        )


def test_register_meeting_rejects_wrong_host_mediaplayer_url(db_conn):
    with pytest.raises(RegistrationValidationError, match="mediaplayer_url"):
        register_meeting(
            db_conn,
            meeting_date=date(2026, 8, 13),
            meeting_title="Wrong MediaPlayer Host Meeting",
            mp3_url=VALID_MP3_URL,
            mediaplayer_url="https://evil.example.com/MediaPlayer.php?view_id=1",
            today=date(2026, 8, 22),
            commit=False,
        )


def test_register_meeting_rejects_stpete_granicus_com_as_mp3_url(db_conn):
    # The exact swapped-fields mistake the host check exists to catch: a
    # MediaPlayer.php URL pasted into the mp3_url field.
    with pytest.raises(RegistrationValidationError, match="mp3_url"):
        register_meeting(
            db_conn,
            meeting_date=date(2026, 8, 13),
            meeting_title="Swapped Fields Meeting",
            mp3_url=VALID_MEDIAPLAYER_URL,
            mediaplayer_url=VALID_MEDIAPLAYER_URL,
            today=date(2026, 8, 22),
            commit=False,
        )


def test_register_meeting_rejects_mp3_url_as_mediaplayer_url(db_conn):
    # The reverse swap direction from the test above: an MP3 URL pasted
    # into the mediaplayer_url field. _validate_host is called
    # symmetrically on both fields, so both directions of the mistake
    # must be caught.
    with pytest.raises(RegistrationValidationError, match="mediaplayer_url"):
        register_meeting(
            db_conn,
            meeting_date=date(2026, 8, 13),
            meeting_title="Swapped Fields Meeting Reverse",
            mp3_url=VALID_MP3_URL,
            mediaplayer_url=VALID_MP3_URL,
            today=date(2026, 8, 22),
            commit=False,
        )


def test_register_meeting_rejects_empty_title(db_conn):
    with pytest.raises(RegistrationValidationError, match="meeting_title"):
        register_meeting(
            db_conn,
            meeting_date=date(2026, 8, 13),
            meeting_title="   ",
            mp3_url=VALID_MP3_URL,
            mediaplayer_url=VALID_MEDIAPLAYER_URL,
            today=date(2026, 8, 22),
            commit=False,
        )


# --- Insert correctness -----------------------------------------------------


def test_register_meeting_persists_all_fields_and_defaults(db_conn):
    meeting_date = date(2026, 8, 13)
    result = register_meeting(
        db_conn,
        meeting_date=meeting_date,
        meeting_title="  City Council Regular Meeting  ",
        mp3_url=VALID_MP3_URL,
        mediaplayer_url=VALID_MEDIAPLAYER_URL,
        today=date(2026, 8, 22),
        commit=False,
    )
    assert result.inserted is True
    assert result.mp3_url == VALID_MP3_URL

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT mp3_url, meeting_title, source_url, published_date, "
            "retrieval_timestamp, status, claimed_at, transcript_text, "
            "failure_reason FROM granicus_transcription_jobs WHERE mp3_url = %s",
            (VALID_MP3_URL,),
        )
        row = cur.fetchone()

    assert row is not None
    (
        mp3_url,
        meeting_title,
        source_url,
        published_date,
        retrieval_timestamp,
        status,
        claimed_at,
        transcript_text,
        failure_reason,
    ) = row

    assert mp3_url == VALID_MP3_URL
    assert meeting_title == "City Council Regular Meeting"  # stripped
    assert source_url == VALID_MEDIAPLAYER_URL  # MediaPlayer.php, attribution only
    assert published_date == meeting_date
    assert retrieval_timestamp is not None
    assert status == "pending"
    assert claimed_at is None
    assert transcript_text is None
    assert failure_reason is None


# --- Idempotency: re-registering the same mp3_url is a safe no-op ----------


def test_register_meeting_is_idempotent_on_mp3_url(db_conn):
    first = register_meeting(
        db_conn,
        meeting_date=date(2026, 8, 13),
        meeting_title="First Registration",
        mp3_url=VALID_MP3_URL,
        mediaplayer_url=VALID_MEDIAPLAYER_URL,
        today=date(2026, 8, 22),
        commit=False,
    )
    assert first.inserted is True

    # Re-register the same mp3_url, even with a different title — a
    # correction path this round doesn't build (DECISIONS #80); the
    # expected behavior here is a safe no-op, not an error and not a
    # silent overwrite.
    second = register_meeting(
        db_conn,
        meeting_date=date(2026, 8, 13),
        meeting_title="Re-registration Attempt With Different Title",
        mp3_url=VALID_MP3_URL,
        mediaplayer_url=VALID_MEDIAPLAYER_URL,
        today=date(2026, 8, 22),
        commit=False,
    )
    assert second.inserted is False

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*), MIN(meeting_title) FROM granicus_transcription_jobs "
            "WHERE mp3_url = %s",
            (VALID_MP3_URL,),
        )
        count, title = cur.fetchone()

    assert count == 1
    assert title == "First Registration"  # original row, untouched


# --- status CHECK constraint is enforced at the DB layer --------------------


def test_status_check_constraint_rejects_unrecognized_status(db_conn):
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO granicus_transcription_jobs "
                "(mp3_url, meeting_title, source_url, published_date, "
                "retrieval_timestamp, status) "
                "VALUES (%s, %s, %s, %s, now(), %s)",
                (
                    "https://archive-video.granicus.com/stpete/bad-status.mp3",
                    "Bad Status Meeting",
                    VALID_MEDIAPLAYER_URL,
                    date(2026, 8, 13),
                    "not_a_real_status",
                ),
            )
    db_conn.rollback()
