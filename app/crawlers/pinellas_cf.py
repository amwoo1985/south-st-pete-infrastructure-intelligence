"""Pinellas Community Foundation grants, index page only.

The only Pinellas CF source, per DECISIONS #11 — do not add any other
pinellascf.org page here without a new DECISIONS.md entry first.

Target (exact page named in DECISIONS #11):
    https://pinellascf.org/nonprofits/grants/

Live-site recon (2026-08-20) confirmed PLAN.md's "5 programs + dated 2026
table" is a single ``<table>`` (the only ``<table>`` element on the page,
wrapped in ``<div class="table-2">``) under an on-page "2026 Grant
Opportunities" heading, with columns GRANT PROGRAM / APPLICATION TIMELINE /
AWARD DISTRIBUTION / MORE INFO and 7 rows spanning 5 distinct program
names (Senior Citizens Services Grants appears 3 times, once per funding
cycle: Housing/Wellness/Support). "AWARD DISTRIBUTION" is a free-text
"[Early ]<Month> <Year>" string in every row observed (e.g. "March 2026",
"Early December 2026") — treated as the item's published/effective date,
parsed to the first of that month. See DECISIONS #29 for why an
unparseable AWARD DISTRIBUTION value is nullable rather than fail-loud.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag

from app.crawlers.base import Attribution, BaseCrawler

GRANTS_URL = "https://pinellascf.org/nonprofits/grants/"

TABLE_CONTAINER_CLASS = "table-2"

EXPECTED_HEADER_TEXT = (
    "GRANT PROGRAM",
    "APPLICATION TIMELINE",
    "AWARD DISTRIBUTION",
    "MORE INFO",
)

_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_AWARD_DATE_RE = re.compile(
    r"\b(" + "|".join(_MONTH_NAMES) + r")\s+(\d{4})\b"
)


def _parse_award_date(text: str) -> date | None:
    """Extracts a first-of-month date from free text like "March 2026" or
    "Early December 2026". Returns None (not a sentinel) when the text
    doesn't contain a recognizable Month-Year pair - e.g. a program whose
    timeline is announced later ("Application window dates announced in
    January 2027 (Invitation Only)" actually does match, but a genuinely
    unannounced value like "TBD" would not) - see DECISIONS #29."""
    match = _AWARD_DATE_RE.search(text)
    if match is None:
        return None
    month_name, year_text = match.groups()
    month = _MONTH_NAMES.index(month_name) + 1
    return date(int(year_text), month, 1)


@dataclass(frozen=True)
class PinellasCFGrantProgram:
    program_name: str
    application_timeline: str
    award_distribution: str
    detail_url: str
    attribution: Attribution


class PinellasCFCrawler(BaseCrawler):
    def __init__(self, **kwargs) -> None:
        super().__init__(source_name="pinellas_cf", **kwargs)

    def crawl(self) -> list[PinellasCFGrantProgram]:
        resp = self.fetch(GRANTS_URL)
        return self.parse_grants_table(resp.text)

    def parse_grants_table(self, html: str) -> list[PinellasCFGrantProgram]:
        soup = BeautifulSoup(html, "lxml")

        table = soup.select_one(f"div.{TABLE_CONTAINER_CLASS} table")
        if table is None:
            self.fail_loud(
                f"div.{TABLE_CONTAINER_CLASS} > table not found on {GRANTS_URL} "
                "- Pinellas CF's grants page structure may have changed"
            )

        header_cells = table.find("thead")
        header_text = header_cells.get_text(" ", strip=True).upper() if header_cells else ""
        missing_headers = [h for h in EXPECTED_HEADER_TEXT if h not in header_text]
        if missing_headers:
            self.fail_loud(
                f"grants table header missing expected column(s) {missing_headers} "
                f"on {GRANTS_URL} - column layout may have changed"
            )

        tbody = table.find("tbody")
        if tbody is None:
            self.fail_loud(f"grants table has no <tbody> on {GRANTS_URL}")

        rows = tbody.find_all("tr", recursive=False)
        if not rows:
            self.fail_loud(f"grants table <tbody> has no rows on {GRANTS_URL}")

        # One retrieval timestamp for the whole page fetch, shared by every
        # program parsed from it - mirrors legistar.py's pattern.
        retrieval_time = datetime.now(timezone.utc)

        programs: list[PinellasCFGrantProgram] = []
        for row in rows:
            programs.append(self._parse_row(row, retrieval_time))
        return programs

    def _parse_row(self, row: Tag, retrieval_time) -> PinellasCFGrantProgram:
        cells = row.find_all("td", recursive=False)
        if len(cells) != 4:
            self.fail_loud(
                f"grants table row has {len(cells)} cells, expected 4 - "
                f"row text: {row.get_text(' ', strip=True)!r}"
            )

        program_name = cells[0].get_text(" ", strip=True)
        application_timeline = cells[1].get_text(" ", strip=True)
        award_distribution = cells[2].get_text(" ", strip=True)

        if not program_name:
            self.fail_loud(f"grants table row has empty GRANT PROGRAM cell on {GRANTS_URL}")

        link = cells[3].find("a")
        if link is None or not link.get("href"):
            self.fail_loud(
                f"grants table row {program_name!r} has no MORE INFO link on {GRANTS_URL}"
            )
        detail_url = urljoin(GRANTS_URL, link.get("href"))

        published_date = _parse_award_date(award_distribution)

        attribution = Attribution(
            source_url=GRANTS_URL,
            retrieval_timestamp=retrieval_time,
            published_date=published_date,
        )

        return PinellasCFGrantProgram(
            program_name=program_name,
            application_timeline=application_timeline,
            award_distribution=award_distribution,
            detail_url=detail_url,
            attribution=attribution,
        )
