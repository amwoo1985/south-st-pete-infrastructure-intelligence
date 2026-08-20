"""Pinellas County BCC meetings via Legistar.

The only Legistar source, per DECISIONS #11 — do not add another Legistar
(or any other) instance here without a new DECISIONS.md entry first.

Target: https://pinellas.legistar.com/Calendar.aspx

Live-site recon (2026-08-20) confirmed the "All Meetings" grid is the
``<table id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00">`` element, with
one 12-column row per meeting in this order: Name, Meeting Date, iCal
export icon, Meeting Time, Meeting Location, Meeting Details, Follow-up
Agenda, Agenda, Accessible Agenda, Minutes, Accessible Minutes, Video.
(The separate ``gridUpcomingMeetings_ctl00`` table nearer the top of the
page duplicates a subset of the same rows by the same meeting IDs — only
``gridCalendar_ctl00`` is parsed, to avoid double-counting.)

Both the Accessible-Agenda-available case and the PDF-only case were
confirmed present in the same live page during recon, and one row with a
`meeting_NotViewable` (closed/non-public meeting) Meeting Details cell was
also observed — that case has no detail URL and is not a parsing failure.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag
from pypdf import PdfReader

from app.crawlers.base import Attribution, BaseCrawler

CALENDAR_URL = "https://pinellas.legistar.com/Calendar.aspx"

CALENDAR_TABLE_ID = "ctl00_ContentPlaceHolder1_gridCalendar_ctl00"

# Header text that must still be present for the parser to trust the
# column layout below. If Legistar reorders/renames columns, this trips
# the fail-loud path instead of silently misreading a different column.
EXPECTED_HEADER_TEXT = (
    "Name",
    "Meeting Date",
    "Meeting Time",
    "Meeting Location",
    "Meeting Details",
    "Agenda",
    "Accessible Agenda",
    "Minutes",
    "Video",
)

# Column order within each <tr>, 0-indexed, confirmed by recon.
COL_NAME = 0
COL_DATE = 1
COL_TIME = 3
COL_LOCATION = 4
COL_MEETING_DETAILS = 5
COL_AGENDA_PDF = 7
COL_ACCESSIBLE_AGENDA_HTML = 8
COL_MINUTES_PDF = 9
COL_ACCESSIBLE_MINUTES_HTML = 10
COL_VIDEO = 11

_VIDEO_ONCLICK_RE = re.compile(r"window\.open\('([^']+)'")


@dataclass(frozen=True)
class LegistarMeeting:
    body_name: str
    meeting_date: date
    meeting_time: str | None
    location: str | None
    meeting_detail_url: str | None  # None for meetings marked "Not viewable by the public"
    agenda_pdf_url: str | None
    accessible_agenda_html_url: str | None
    minutes_pdf_url: str | None
    accessible_minutes_html_url: str | None
    video_url: str | None
    agenda_text: str | None  # populated by resolve_agenda_content()
    agenda_source: str | None  # "accessible_html" | "pdf" | None
    attribution: Attribution


def _cell_link_url(cell: Tag) -> str | None:
    """An <a> with a real href means the item is available; the
    "Not available" state renders an <a> with no href (or href="#") and a
    *NotAvail*/*NotViewable* class instead."""
    a = cell.find("a")
    if a is None:
        return None
    href = a.get("href")
    if not href or href == "#":
        return None
    return urljoin(CALENDAR_URL, href)


def _cell_video_url(cell: Tag) -> str | None:
    """Video links render as onclick=\"window.open('Video.aspx?...', ...)\"
    rather than a plain href."""
    a = cell.find("a")
    if a is None:
        return None
    onclick = a.get("onclick")
    if not onclick:
        return None
    match = _VIDEO_ONCLICK_RE.search(onclick)
    if not match:
        return None
    return urljoin(CALENDAR_URL, match.group(1))


class LegistarCrawler(BaseCrawler):
    def __init__(self, **kwargs) -> None:
        super().__init__(source_name="legistar", **kwargs)

    def crawl(self, resolve_agenda: bool = True) -> list[LegistarMeeting]:
        resp = self.fetch(CALENDAR_URL)
        meetings = self.parse_calendar(resp.text)
        if resolve_agenda:
            meetings = [self.resolve_agenda_content(m) for m in meetings]
        return meetings

    def parse_calendar(self, html: str) -> list[LegistarMeeting]:
        soup = BeautifulSoup(html, "lxml")

        table = soup.find(id=CALENDAR_TABLE_ID)
        if table is None:
            self.fail_loud(
                f"calendar table #{CALENDAR_TABLE_ID} not found on {CALENDAR_URL} "
                "- Legistar's page structure may have changed"
            )

        header_text = table.find("thead").get_text(" ", strip=True) if table.find("thead") else ""
        missing_headers = [h for h in EXPECTED_HEADER_TEXT if h not in header_text]
        if missing_headers:
            self.fail_loud(
                f"calendar table header missing expected column(s) {missing_headers} "
                f"on {CALENDAR_URL} - column layout may have changed"
            )

        tbody = table.find("tbody")
        if tbody is None:
            self.fail_loud(f"calendar table has no <tbody> on {CALENDAR_URL}")

        rows = tbody.find_all("tr", recursive=False)
        # One retrieval timestamp for the whole page fetch, shared by every
        # meeting parsed from it - "when this system actually fetched it"
        # is the calendar-page request, not each row's parse time.
        retrieval_time = datetime.now(timezone.utc)

        meetings: list[LegistarMeeting] = []
        for row in rows:
            meetings.append(self._parse_row(row, retrieval_time))
        return meetings

    def _parse_row(self, row: Tag, retrieval_time: datetime) -> LegistarMeeting:
        cells = row.find_all("td", recursive=False)
        if len(cells) < 12:
            self.fail_loud(
                f"calendar row has {len(cells)} cells, expected 12 - "
                f"row markup: {row.get('id', '(no id)')}"
            )

        body_name = cells[COL_NAME].get_text(" ", strip=True)

        # Unlike Meeting Details (below), Legistar always populates this
        # column for any row it emits - there's no observed or documented
        # "meeting exists but date isn't set yet" state for this grid. An
        # unparseable date here means the format changed, not that the
        # date is legitimately unknown, so this fails loud rather than
        # falling back to a nullable published_date.
        date_text = cells[COL_DATE].get_text(" ", strip=True)
        try:
            meeting_date = datetime.strptime(date_text, "%m/%d/%Y").date()
        except ValueError:
            self.fail_loud(
                f"could not parse meeting date {date_text!r} - expected M/D/YYYY format"
            )

        meeting_time = cells[COL_TIME].get_text(" ", strip=True) or None
        location = cells[COL_LOCATION].get_text(" ", strip=True) or None

        detail_link = cells[COL_MEETING_DETAILS].find("a")
        meeting_detail_url = None
        if detail_link is not None:
            href = detail_link.get("href")
            if href:
                meeting_detail_url = urljoin(CALENDAR_URL, href)
        # No href on Meeting Details is expected for "Not viewable by the
        # public" meetings (confirmed in recon) - not a structure failure.

        agenda_pdf_url = _cell_link_url(cells[COL_AGENDA_PDF])
        accessible_agenda_html_url = _cell_link_url(cells[COL_ACCESSIBLE_AGENDA_HTML])
        minutes_pdf_url = _cell_link_url(cells[COL_MINUTES_PDF])
        accessible_minutes_html_url = _cell_link_url(cells[COL_ACCESSIBLE_MINUTES_HTML])
        video_url = _cell_video_url(cells[COL_VIDEO])

        source_url = meeting_detail_url or CALENDAR_URL
        attribution = Attribution(
            source_url=source_url,
            retrieval_timestamp=retrieval_time,
            published_date=meeting_date,
        )

        return LegistarMeeting(
            body_name=body_name,
            meeting_date=meeting_date,
            meeting_time=meeting_time,
            location=location,
            meeting_detail_url=meeting_detail_url,
            agenda_pdf_url=agenda_pdf_url,
            accessible_agenda_html_url=accessible_agenda_html_url,
            minutes_pdf_url=minutes_pdf_url,
            accessible_minutes_html_url=accessible_minutes_html_url,
            video_url=video_url,
            agenda_text=None,
            agenda_source=None,
            attribution=attribution,
        )

    def resolve_agenda_content(self, meeting: LegistarMeeting) -> LegistarMeeting:
        """Prefer the Accessible-Agenda HTML view; fall back to PDF text
        extraction only when HTML isn't offered for that meeting. If
        neither is offered, that's a legitimate "no agenda posted yet"
        state, not a failure."""
        if meeting.accessible_agenda_html_url:
            resp = self.fetch(meeting.accessible_agenda_html_url)
            text = self._extract_html_text(resp.text, meeting.accessible_agenda_html_url)
            return replace(meeting, agenda_text=text, agenda_source="accessible_html")

        if meeting.agenda_pdf_url:
            resp = self.fetch(meeting.agenda_pdf_url)
            text = self._extract_pdf_text(resp.content, meeting.agenda_pdf_url)
            return replace(meeting, agenda_text=text, agenda_source="pdf")

        return meeting

    def _extract_html_text(self, html: str, url: str) -> str:
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(separator="\n", strip=True)
        if not text:
            self.fail_loud(f"Accessible Agenda HTML at {url} parsed to empty text")
        return text

    def _extract_pdf_text(self, pdf_bytes: bytes, url: str) -> str:
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            parts = [page.extract_text() or "" for page in reader.pages]
        except Exception as exc:  # pypdf raises various exceptions on malformed PDFs
            self.fail_loud(f"could not parse PDF at {url}: {exc}")
        text = "\n".join(parts).strip()
        if not text:
            self.fail_loud(f"PDF at {url} produced no extractable text")
        return text
