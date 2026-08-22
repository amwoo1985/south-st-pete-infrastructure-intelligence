"""CLI for registering St. Petersburg City Council meetings into the
Granicus transcription jobs table (DECISIONS #79-82) — the human-input
mechanism that replaces the blocked automated RSS/MediaPlayer.php
discovery (DECISIONS #77). Amber runs this periodically, supplying a
meeting's date/title/direct-MP3-URL/MediaPlayer.php-URL after resolving
them herself via a real manual browser session on stpete.granicus.com —
this script never fetches either URL.

Two usage modes:

  Single meeting:
    python scripts/register_granicus_meeting.py \\
        --meeting-date 2026-08-13 \\
        --meeting-title "City Council Regular Meeting" \\
        --mp3-url https://archive-video.granicus.com/stpete/<uuid>.mp3 \\
        --mediaplayer-url "https://stpete.granicus.com/MediaPlayer.php?view_id=X&clip_id=Y"

  Batch, from a JSON file (a list of objects with the same 4 keys,
  snake_case: meeting_date, meeting_title, mp3_url, mediaplayer_url):
    python scripts/register_granicus_meeting.py --file meetings.json

Rejects (does not insert) any meeting older than DECISIONS #12's 12-month
backfill bound, or dated in the future. Batch mode continues past one bad
row rather than aborting the whole file (mirrors
embed_and_insert_chunks()'s per-item continue-on-failure pattern,
DECISIONS #71) — prints one OK/SKIP/FAIL line per row, exits nonzero if
any row failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.connection import get_connection
from app.granicus.register import (
    BackfillBoundError,
    RegistrationValidationError,
    register_meeting,
)
from app.granicus.schema import apply_granicus_schema


def _register_one(conn, row: dict) -> bool:
    """Registers one meeting from a dict of raw string fields (CLI flags or
    one JSON batch-file row). All four required-key lookups AND the date
    parse happen inside this function's own try/except, not at the call
    site — a batch row missing a key or holding an unparseable date must
    report FAIL and let the caller continue to the next row, never raise
    an uncaught KeyError that aborts the rest of the file."""
    try:
        meeting_title = row["meeting_title"]
        mp3_url = row["mp3_url"]
        mediaplayer_url = row["mediaplayer_url"]
        meeting_date_str = row["meeting_date"]
    except KeyError as exc:
        print(f"FAIL  malformed row (missing key {exc}): {row!r}")
        return False

    try:
        meeting_date = date.fromisoformat(meeting_date_str)
    except ValueError as exc:
        print(f"FAIL  {meeting_title!r}: invalid meeting_date {meeting_date_str!r} ({exc})")
        return False

    try:
        result = register_meeting(
            conn,
            meeting_date=meeting_date,
            meeting_title=meeting_title,
            mp3_url=mp3_url,
            mediaplayer_url=mediaplayer_url,
        )
    except (BackfillBoundError, RegistrationValidationError) as exc:
        print(f"FAIL  {meeting_title!r}: {exc}")
        return False

    if result.inserted:
        print(f"OK    {meeting_title!r} ({meeting_date.isoformat()}) registered as pending")
    else:
        print(f"SKIP  {meeting_title!r} ({meeting_date.isoformat()}) already registered")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register a Granicus meeting for transcription (DECISIONS #79-82)."
    )
    parser.add_argument("--meeting-date", help="YYYY-MM-DD")
    parser.add_argument("--meeting-title")
    parser.add_argument("--mp3-url", help="Direct archive-video.granicus.com MP3 URL")
    parser.add_argument(
        "--mediaplayer-url",
        help="stpete.granicus.com MediaPlayer.php URL (attribution only, never fetched)",
    )
    parser.add_argument(
        "--file",
        type=Path,
        help="JSON file: a list of {meeting_date, meeting_title, mp3_url, mediaplayer_url}",
    )
    args = parser.parse_args()

    single_flags = [args.meeting_date, args.meeting_title, args.mp3_url, args.mediaplayer_url]
    if args.file and any(single_flags):
        parser.error("--file is exclusive with the single-meeting flags")
    if not args.file and not all(single_flags):
        parser.error(
            "supply either --file or all of --meeting-date/--meeting-title/"
            "--mp3-url/--mediaplayer-url"
        )

    conn = get_connection()
    apply_granicus_schema(conn)

    all_ok = True
    try:
        if args.file:
            rows = json.loads(args.file.read_text(encoding="utf-8"))
            for row in rows:
                ok = _register_one(conn, row)
                all_ok = all_ok and ok
        else:
            all_ok = _register_one(
                conn,
                {
                    "meeting_date": args.meeting_date,
                    "meeting_title": args.meeting_title,
                    "mp3_url": args.mp3_url,
                    "mediaplayer_url": args.mediaplayer_url,
                },
            )
    finally:
        conn.close()

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
