"""Tests for app/chunking/legistar.py.

Uses the real recorded Accessible-Agenda fixture
(tests/fixtures/legistar/accessible_agenda.html, the same one
tests/crawlers/test_legistar.py verifies against) run through the real
crawler's own text-extraction method
(LegistarCrawler._extract_html_text()) — the same real agenda_text a live
crawl would produce — rather than a hand-written approximation of what
Legistar agenda text looks like. See DECISIONS #59 for why the item-
boundary heuristic needed a stub-merge pass; this suite is what verified
that against real data before it was written into the module.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

from app.chunking.base import Chunk
from app.chunking.legistar import (
    DOC_TYPE_AGENDA_ITEM,
    DOC_TYPE_AGENDA_WHOLE,
    chunk_legistar_meeting,
    chunk_legistar_meetings,
)
from app.crawlers.base import Attribution
from app.crawlers.legistar import LegistarCrawler, LegistarMeeting

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "legistar"
ACCESSIBLE_AGENDA_URL = "https://pinellas.legistar.com/View.ashx?M=A&ID=1249432"


def load_real_agenda_text() -> str:
    html = (FIXTURES_DIR / "accessible_agenda.html").read_text(encoding="utf-8")
    crawler = LegistarCrawler(min_request_interval_seconds=0)
    return crawler._extract_html_text(html, ACCESSIBLE_AGENDA_URL)


def make_meeting(**overrides) -> LegistarMeeting:
    base = dict(
        body_name="Board of County Commissioners",
        meeting_date=date(2025, 12, 16),
        meeting_time="2:00 PM",
        location="333 Chestnut Street, Palm Room",
        meeting_detail_url="https://pinellas.legistar.com/MeetingDetail.aspx?ID=1249432",
        agenda_pdf_url=None,
        accessible_agenda_html_url=ACCESSIBLE_AGENDA_URL,
        minutes_pdf_url=None,
        accessible_minutes_html_url=None,
        video_url=None,
        agenda_text=None,
        agenda_source=None,
        attribution=Attribution.now(
            source_url="https://pinellas.legistar.com/MeetingDetail.aspx?ID=1249432",
            published_date=date(2025, 12, 16),
        ),
    )
    base.update(overrides)
    return LegistarMeeting(**base)


# --- Real-fixture item-boundary chunking ------------------------------------


def test_real_agenda_chunks_by_item_not_whole_document():
    meeting = make_meeting(agenda_text=load_real_agenda_text(), agenda_source="accessible_html")
    chunks = chunk_legistar_meeting(meeting)

    assert len(chunks) > 1  # real structure detected, not a whole-doc fallback
    assert all(c.doc_type == DOC_TYPE_AGENDA_ITEM for c in chunks)


def test_real_agenda_chunks_have_no_stub_items():
    # DECISIONS #59: Legistar's flattened consent-agenda table layout
    # produces "stub" item boundaries (a marker line with nothing else
    # before the next boundary) that must be merged forward — none should
    # survive into the final chunk list.
    meeting = make_meeting(agenda_text=load_real_agenda_text(), agenda_source="accessible_html")
    chunks = chunk_legistar_meeting(meeting)

    for chunk in chunks:
        # A stub's un-merged text would be exactly "<number>." with nothing
        # else — every real chunk must carry substantially more.
        assert len(chunk.text) > 10


def test_real_agenda_item_1_chunk_contains_expected_content():
    meeting = make_meeting(agenda_text=load_real_agenda_text(), agenda_source="accessible_html")
    chunks = chunk_legistar_meeting(meeting)

    item_1 = next(c for c in chunks if c.section_label and "Item 1" in c.section_label and "Item 1-" not in c.section_label and "Item 10" not in c.section_label)
    assert "Citizens To Be Heard" in item_1.text


def test_real_agenda_chunk_attribution_passes_through():
    meeting = make_meeting(agenda_text=load_real_agenda_text(), agenda_source="accessible_html")
    chunks = chunk_legistar_meeting(meeting)

    for chunk in chunks:
        assert chunk.attribution is meeting.attribution
        assert chunk.attribution.source_url == meeting.attribution.source_url
        assert chunk.attribution.published_date == date(2025, 12, 16)


# --- Determinism / idempotency ----------------------------------------------


def test_chunking_the_same_meeting_twice_produces_identical_ids():
    meeting = make_meeting(agenda_text=load_real_agenda_text(), agenda_source="accessible_html")
    ids_1 = [c.chunk_id for c in chunk_legistar_meeting(meeting)]
    ids_2 = [c.chunk_id for c in chunk_legistar_meeting(meeting)]
    assert ids_1 == ids_2
    assert len(ids_1) == len(set(ids_1))  # no duplicate IDs within one meeting


def test_two_different_meetings_with_the_same_calendar_fallback_url_do_not_collide():
    # DECISIONS #21: "not viewable" meetings fall back to sharing
    # CALENDAR_URL as source_url. Two such meetings on different dates
    # must not produce colliding item-1 chunk IDs.
    shared_url = "https://pinellas.legistar.com/Calendar.aspx"
    meeting_a = make_meeting(
        meeting_date=date(2025, 1, 1),
        agenda_text="1.\n25-001\n1A\nClosed session item A.",
        attribution=Attribution.now(source_url=shared_url, published_date=date(2025, 1, 1)),
    )
    meeting_b = make_meeting(
        meeting_date=date(2025, 2, 1),
        agenda_text="1.\n25-002\n1A\nClosed session item B.",
        attribution=Attribution.now(source_url=shared_url, published_date=date(2025, 2, 1)),
    )
    chunks_a = chunk_legistar_meeting(meeting_a)
    chunks_b = chunk_legistar_meeting(meeting_b)
    assert chunks_a[0].chunk_id != chunks_b[0].chunk_id


# --- No agenda_text: nothing to chunk yet -----------------------------------


def test_no_agenda_text_produces_no_chunks():
    meeting = make_meeting(agenda_text=None)
    assert chunk_legistar_meeting(meeting) == []


# --- Whole-agenda fallback: no detectable item structure --------------------


def test_agenda_text_without_item_structure_falls_back_to_one_whole_chunk():
    meeting = make_meeting(agenda_text="Just a short note with no item markers at all.")
    chunks = chunk_legistar_meeting(meeting)

    assert len(chunks) == 1
    assert chunks[0].doc_type == DOC_TYPE_AGENDA_WHOLE
    assert chunks[0].text == meeting.agenda_text


def test_bare_digit_period_line_without_a_file_number_does_not_trigger_a_false_boundary():
    # A numbered sub-list inside an item's own recommendation text (e.g.
    # "1. Approve the ranking... 2. Authorize the Chair...") must not be
    # mistaken for a real Legistar item boundary — only a marker line
    # confirmed by a nearby file-number line counts.
    text = (
        "10.\n25-1234\n5A\nSome recommendation with sub-steps:\n"
        "1.\nApprove the ranking of firms.\n2.\nAuthorize the Chair to execute."
    )
    meeting = make_meeting(agenda_text=text)
    chunks = chunk_legistar_meeting(meeting)

    assert len(chunks) == 1
    assert chunks[0].doc_type == DOC_TYPE_AGENDA_ITEM
    assert "Authorize the Chair" in chunks[0].text


# --- Multiple meetings -------------------------------------------------------


def test_chunk_legistar_meetings_flattens_across_meetings():
    meeting_a = make_meeting(
        meeting_date=date(2025, 1, 1),
        agenda_text="1.\n25-001\n1A\nItem A text.",
        attribution=Attribution.now(
            source_url="https://pinellas.legistar.com/MeetingDetail.aspx?ID=1", published_date=date(2025, 1, 1)
        ),
    )
    meeting_b = make_meeting(
        meeting_date=date(2025, 2, 1),
        agenda_text="1.\n25-002\n1A\nItem B text.",
        attribution=Attribution.now(
            source_url="https://pinellas.legistar.com/MeetingDetail.aspx?ID=2", published_date=date(2025, 2, 1)
        ),
    )
    no_agenda_meeting = make_meeting(meeting_date=date(2025, 3, 1), agenda_text=None)

    chunks = chunk_legistar_meetings([meeting_a, meeting_b, no_agenda_meeting])
    assert len(chunks) == 2
    assert {c.text for c in chunks} == {
        "1. 25-001 1A Item A text.",
        "1. 25-002 1A Item B text.",
    }
