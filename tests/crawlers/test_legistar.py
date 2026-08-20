"""Tests for app/crawlers/legistar.py.

Uses real recorded fixtures (tests/fixtures/legistar/) captured from
pinellas.legistar.com during this session — see DECISIONS.md for the
fixture-recording note and why "This Month" (upcoming) alone couldn't
exercise the minutes-link path. Structure-failure (fail-loud) cases use
small hand-built synthetic HTML instead of a real fixture, since there's
no live page in a "broken" state to record.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest
import responses

from app.crawlers.base import Attribution, CrawlerStructureError
from app.crawlers.legistar import (
    CALENDAR_TABLE_ID,
    CALENDAR_URL,
    LegistarCrawler,
    LegistarMeeting,
)
from tests.conftest import load_fixture_bytes, load_fixture_text, register_robots_permissive


def make_crawler() -> LegistarCrawler:
    # min_request_interval_seconds=0 - these are unit tests against mocked
    # HTTP, not a live-rate-limiting test (that's covered separately in
    # test_base.py against a mocked clock).
    return LegistarCrawler(min_request_interval_seconds=0)


def make_meeting(**overrides) -> LegistarMeeting:
    base = dict(
        body_name="Board of County Commissioners",
        meeting_date=date(2025, 12, 16),
        meeting_time="2:00 PM",
        location="333 Chestnut Street",
        meeting_detail_url="https://pinellas.legistar.com/MeetingDetail.aspx?ID=1249432",
        agenda_pdf_url=None,
        accessible_agenda_html_url=None,
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


# --- Fail-loud: structure-parsing failures ---------------------------------


def test_parse_calendar_missing_table_raises():
    html = "<html><body><p>Legistar redesigned this page</p></body></html>"
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="calendar table"):
        crawler.parse_calendar(html)


def test_parse_calendar_renamed_headers_raises():
    html = f"""
    <html><body>
    <table id="{CALENDAR_TABLE_ID}">
      <thead><tr><th>Name</th><th>When</th></tr></thead>
      <tbody><tr><td>x</td></tr></tbody>
    </table>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="missing expected column"):
        crawler.parse_calendar(html)


def test_parse_calendar_missing_tbody_raises():
    html = f"""
    <html><body>
    <table id="{CALENDAR_TABLE_ID}">
      <thead><tr>
        <th>Name</th><th>Meeting Date</th><th>Meeting Time</th>
        <th>Meeting Location</th><th>Meeting Details</th><th>Agenda</th>
        <th>Accessible Agenda</th><th>Minutes</th><th>Video</th>
      </tr></thead>
    </table>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="no <tbody>"):
        crawler.parse_calendar(html)


# --- Real-fixture parsing: shape and attribution ---------------------------


def test_parse_calendar_this_month_real_fixture():
    html = load_fixture_text("calendar_this_month.html")
    crawler = make_crawler()
    meetings = crawler.parse_calendar(html)

    assert len(meetings) > 0
    for m in meetings:
        assert m.body_name
        assert isinstance(m.meeting_date, date)
        # Mandatory attribution per .claude/rules/crawler.md: source URL +
        # published/effective date + retrieval timestamp on every item.
        assert m.attribution.source_url
        assert m.attribution.published_date == m.meeting_date
        assert isinstance(m.attribution.retrieval_timestamp, datetime)
        assert m.attribution.retrieval_timestamp.tzinfo is not None


# --- Gap 1: minutes-link extraction against a real past-meetings fixture ---


def test_parse_calendar_2025_minutes_link_populated():
    """The default "This Month" view had zero meetings with posted minutes
    (nothing had happened yet) - this fixture is the year-2025 postback
    result, which does have meetings old enough to have minutes posted.
    Confirms _cell_link_url actually returns a real minutes URL, not just
    that the code path exists."""
    html = load_fixture_text("calendar_2025.html")
    crawler = make_crawler()
    meetings = crawler.parse_calendar(html)

    with_minutes = [m for m in meetings if m.minutes_pdf_url]
    assert len(with_minutes) > 0, "expected at least one 2025 meeting with a posted minutes PDF"

    sample = with_minutes[0]
    assert sample.minutes_pdf_url.startswith("https://pinellas.legistar.com/View.ashx?M=M")
    assert sample.attribution.source_url
    assert sample.attribution.published_date is not None


def test_date_range_postback_rejects_unknown_value():
    crawler = make_crawler()
    with pytest.raises(ValueError, match="unknown date_range"):
        crawler._fetch_calendar_for_date_range("Whenever")


@responses.activate
def test_crawl_date_range_performs_get_then_post():
    """crawl(date_range="2025") should GET the calendar once to capture
    viewstate, then POST the filtered search - not a bare GET with a query
    string (Legistar's date filter isn't a query param, it's an ASP.NET
    WebForms postback)."""
    register_robots_permissive(responses)
    responses.add(
        responses.GET,
        CALENDAR_URL,
        body=load_fixture_text("calendar_this_month.html"),
        status=200,
    )
    responses.add(
        responses.POST,
        CALENDAR_URL,
        body=load_fixture_text("calendar_2025.html"),
        status=200,
    )

    crawler = make_crawler()
    meetings = crawler.crawl(date_range="2025", resolve_agenda=False)

    get_calls = [c for c in responses.calls if c.request.method == "GET" and c.request.url == CALENDAR_URL]
    post_calls = [c for c in responses.calls if c.request.method == "POST" and c.request.url == CALENDAR_URL]
    assert len(get_calls) == 1
    assert len(post_calls) == 1
    assert any(m.minutes_pdf_url for m in meetings)

    # The postback body must carry the real viewstate/eventvalidation
    # snapshot, not just the year override - otherwise Legistar's server
    # would reject it as an invalid postback.
    posted_body = post_calls[0].request.body
    assert "__VIEWSTATE=" in posted_body
    assert "__EVENTVALIDATION=" in posted_body


@responses.activate
def test_crawl_date_range_fails_loud_if_filter_did_not_take_effect():
    """If Legistar's response doesn't echo back the requested date_range in
    lstYears_Input (e.g. the control was renamed and the postback silently
    no-op'd), this must raise rather than quietly returning the wrong
    range's data mislabeled as the requested one."""
    register_robots_permissive(responses)
    responses.add(
        responses.GET,
        CALENDAR_URL,
        body=load_fixture_text("calendar_this_month.html"),
        status=200,
    )
    responses.add(
        responses.POST,
        CALENDAR_URL,
        # Server echoes back "This Month" - a stale/unchanged filter -
        # instead of the requested "2025".
        body=load_fixture_text("calendar_this_month.html"),
        status=200,
    )

    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="did not take effect"):
        crawler.crawl(date_range="2025", resolve_agenda=False)


def test_extract_postback_fields_captures_viewstate_and_skips_submit_buttons():
    from bs4 import BeautifulSoup

    html = load_fixture_text("calendar_this_month.html")
    soup = BeautifulSoup(html, "lxml")
    form = soup.find("form")

    fields = LegistarCrawler._extract_postback_fields(form)

    assert "__VIEWSTATE" in fields and fields["__VIEWSTATE"]
    assert "__EVENTVALIDATION" in fields and fields["__EVENTVALIDATION"]
    # Submit buttons (type="submit") must be excluded - only the one the
    # caller explicitly clicks (btnSearch) gets added back deliberately.
    assert "ctl00$ContentPlaceHolder1$btnSearch" not in fields


# --- Accessible-Agenda-HTML preferred over PDF ------------------------------


@responses.activate
def test_resolve_agenda_content_prefers_accessible_html():
    accessible_url = "https://pinellas.legistar.com/View.ashx?M=AADA&ID=1249432&GUID=X"
    register_robots_permissive(responses)
    responses.add(
        responses.GET,
        accessible_url,
        body=load_fixture_text("accessible_agenda.html"),
        status=200,
    )
    # Deliberately no mock registered for a PDF URL - if the code fell
    # back to PDF despite HTML being available, `responses` would raise a
    # ConnectionError for the unregistered call and fail this test.
    meeting = make_meeting(
        accessible_agenda_html_url=accessible_url,
        agenda_pdf_url="https://pinellas.legistar.com/View.ashx?M=A&ID=1249432&GUID=X",
    )

    crawler = make_crawler()
    resolved = crawler.resolve_agenda_content(meeting)

    assert resolved.agenda_source == "accessible_html"
    assert "Board of County Commissioners" in resolved.agenda_text
    assert "Agenda" in resolved.agenda_text


# --- PDF fallback ------------------------------------------------------------


@responses.activate
def test_resolve_agenda_content_falls_back_to_pdf_when_no_accessible_html():
    pdf_url = "https://pinellas.legistar.com/View.ashx?M=A&ID=1422856&GUID=X"
    register_robots_permissive(responses)
    responses.add(
        responses.GET,
        pdf_url,
        body=load_fixture_bytes("agenda_fallback.pdf"),
        status=200,
        content_type="application/pdf",
    )
    meeting = make_meeting(accessible_agenda_html_url=None, agenda_pdf_url=pdf_url)

    crawler = make_crawler()
    resolved = crawler.resolve_agenda_content(meeting)

    assert resolved.agenda_source == "pdf"
    assert "AGENDA" in resolved.agenda_text.upper()


def test_resolve_agenda_content_neither_offered_is_not_a_failure():
    """No agenda posted yet is a legitimate state (DECISIONS-aligned), not
    a parser failure - resolve_agenda_content must return the meeting
    unchanged, not raise."""
    meeting = make_meeting(accessible_agenda_html_url=None, agenda_pdf_url=None)
    crawler = make_crawler()
    resolved = crawler.resolve_agenda_content(meeting)
    assert resolved.agenda_text is None
    assert resolved.agenda_source is None
