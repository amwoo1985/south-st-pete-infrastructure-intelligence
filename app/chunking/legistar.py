"""Chunking for Pinellas County BCC Legistar meetings
(app/crawlers/legistar.py's LegistarMeeting).

Only ``agenda_text`` is chunked — it's the only free-text field the
crawler actually populates today (``resolve_agenda_content()`` fills
``agenda_text`` from either the Accessible-Agenda HTML view or a PDF
fallback; ``minutes_pdf_url``/``accessible_minutes_html_url`` are stored as
URLs but their content is never extracted by the crawler, so there is
nothing to chunk there yet).

Domain-aware boundary: chunk by agenda item, not fixed-length splitting,
per .claude/rules/rag.md. See DECISIONS #59 for the item-boundary heuristic
and why it needed a stub-merge pass — this was verified against the real
recorded fixture (tests/fixtures/legistar/accessible_agenda.html), not
designed in the abstract.

If a meeting's agenda_text has no detectable item-boundary structure (a
differently-formatted agenda, or a PDF-fallback extraction with different
line breaks), this falls back to one whole-agenda chunk rather than forcing
a boundary onto text that doesn't have it, and rather than fixed-length
splitting a dense meeting-agenda document (rag.md: "don't split a clause
mid-thought with naive fixed-length chunking without checking").
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.chunking.base import Chunk, make_chunk_id
from app.crawlers.legistar import LegistarMeeting

DOC_TYPE_AGENDA_ITEM = "legistar_agenda_item"
DOC_TYPE_AGENDA_WHOLE = "legistar_agenda_whole"

# A line holding only an item number ("10.") — the first half of Legistar's
# standard agenda item header. Deliberately not sufficient on its own (see
# _FILE_NUMBER_RE below) since a numbered list inside an item's own body
# text (e.g. a "1. Approve the ranking..." sub-list within a
# recommendation) would otherwise produce a false boundary.
_ITEM_MARKER_RE = re.compile(r"^\d{1,3}\.$")

# A line holding a Legistar file number ("25-177") — confirms an
# _ITEM_MARKER_RE line is a real item header rather than an incidental
# digit+period line. Legistar file numbers are "YY-NNNN"; the live fixture
# also shows this pattern surviving even where the DOCX-to-HTML text
# extraction has split a 4-digit number's last character onto the next
# line (e.g. "25-152" / "5A" — the real file number is "25-1525", version
# "A", split mid-digit by the source markup's line-wrapping) — the
# 2-4-digit range here tolerates both the whole and the split-truncated
# form without over-matching.
_FILE_NUMBER_RE = re.compile(r"^\d{2}-\d{2,4}$")

# How many lines ahead of an item-marker line to look for a confirming
# file-number line. 3 was the minimum window that captured every real
# boundary in the recorded fixture without over-matching.
_FILE_NUMBER_LOOKAHEAD = 3


def _split_agenda_into_items(agenda_text: str) -> list[tuple[str, str]]:
    """Best-effort split of a Legistar Accessible-Agenda plain-text blob
    into (item_number, item_text) pairs. Returns an empty list if no item
    boundaries are found at all — callers fall back to a whole-agenda
    chunk rather than force this structure onto text that doesn't have it.

    See DECISIONS #59: live recon against the real fixture found Legistar's
    own table-based consent-agenda layout sometimes flattens (via
    BeautifulSoup's get_text()) into item-number cells, file-number cells,
    and description cells each grouped separately rather than interleaved
    per item — e.g. items 2/3/4's file numbers and descriptions all land
    inside what looks like item 3's or item 6's block, leaving items 2, 4,
    5 as "stub" boundaries with no content of their own before the next
    boundary. _merge_stub_items() below folds those stubs forward into the
    next real content so no chunk is left empty."""
    lines = agenda_text.splitlines()
    boundary_indices: list[int] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not _ITEM_MARKER_RE.match(stripped):
            continue
        window = lines[i + 1 : i + 1 + _FILE_NUMBER_LOOKAHEAD]
        if any(_FILE_NUMBER_RE.match(w.strip()) for w in window):
            boundary_indices.append(i)

    if not boundary_indices:
        return []

    raw_items: list[tuple[str, str]] = []
    for idx, start in enumerate(boundary_indices):
        end = boundary_indices[idx + 1] if idx + 1 < len(boundary_indices) else len(lines)
        item_number = lines[start].strip().rstrip(".")
        block_lines = lines[start:end]
        text = " ".join(l.strip() for l in block_lines if l.strip())
        raw_items.append((item_number, text))

    return _merge_stub_items(raw_items)


def _merge_stub_items(raw_items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Folds a "stub" item — one whose captured text is literally nothing
    but its own marker (e.g. item "2." whose block is exactly "2." because
    item 3's boundary immediately follows with no content in between) —
    forward into the next non-stub item. The merged item's label spans the
    stub's number(s) through the item that actually carries the content
    (e.g. "2-3"), so a citation still names every item number the chunk
    covers rather than silently dropping the stub numbers."""
    merged: list[tuple[str, str]] = []
    pending_numbers: list[str] = []

    for item_number, text in raw_items:
        is_stub = text.strip() == f"{item_number}."
        if is_stub:
            pending_numbers.append(item_number)
            continue
        numbers = [*pending_numbers, item_number]
        label = numbers[0] if len(numbers) == 1 else f"{numbers[0]}-{numbers[-1]}"
        merged.append((label, text))
        pending_numbers = []

    if pending_numbers:
        # Trailing stub(s) with nothing after them on the whole page —
        # keep as their own thin chunk rather than silently dropping the
        # item number(s) entirely.
        label = pending_numbers[0] if len(pending_numbers) == 1 else f"{pending_numbers[0]}-{pending_numbers[-1]}"
        merged.append((label, f"Item {label}."))

    return merged


def chunk_legistar_meeting(meeting: LegistarMeeting) -> list[Chunk]:
    """Chunks one LegistarMeeting's agenda_text. Returns an empty list if
    no agenda_text is present (no agenda posted yet, or the meeting is
    "not viewable by the public" per DECISIONS #21) — that's a legitimate
    "nothing to chunk yet" state, not a failure."""
    if not meeting.agenda_text:
        return []

    source_url = meeting.attribution.source_url
    meeting_date_str = meeting.meeting_date.isoformat()
    items = _split_agenda_into_items(meeting.agenda_text)

    if not items:
        chunk_id = make_chunk_id(
            source_url, meeting.body_name, meeting_date_str, "agenda", "whole"
        )
        label = f"{meeting.body_name} — {meeting_date_str} (full agenda)"
        return [
            Chunk(
                chunk_id=chunk_id,
                doc_type=DOC_TYPE_AGENDA_WHOLE,
                text=meeting.agenda_text,
                section_label=label,
                attribution=meeting.attribution,
            )
        ]

    chunks: list[Chunk] = []
    for item_label, text in items:
        # source_url can be shared across multiple "not viewable" meetings
        # (DECISIONS #21's calendar-page fallback) — body_name and
        # meeting_date are folded into the identity too so two different
        # meetings' item-1 chunks can never collide.
        chunk_id = make_chunk_id(
            source_url, meeting.body_name, meeting_date_str, "agenda_item", item_label
        )
        label = f"{meeting.body_name} — {meeting_date_str}, Item {item_label}"
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                doc_type=DOC_TYPE_AGENDA_ITEM,
                text=text,
                section_label=label,
                attribution=meeting.attribution,
            )
        )
    return chunks


def chunk_legistar_meetings(meetings: Iterable[LegistarMeeting]) -> list[Chunk]:
    """Chunks a whole crawl's worth of meetings."""
    chunks: list[Chunk] = []
    for meeting in meetings:
        chunks.extend(chunk_legistar_meeting(meeting))
    return chunks
