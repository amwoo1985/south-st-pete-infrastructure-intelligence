"""stpete.org DECISIONS #43 income_limits.php - the shared AMI (Area Median
Income) eligibility-threshold reference table that several DECISIONS #35
per-program detail pages (``for_property_owners.php``,
``housing_rehabilitation_assistance_program.php``,
``purchase_assistance_program.php``,
``rebates_for_affordable_residential_rehabs.php``) cite by AMI percentage
(e.g. "80% AMI") without ever stating the dollar figure themselves.

Scope is exactly this 1 URL DECISIONS #43 names - do not add another one
without a new DECISIONS.md entry. No internal stpete.org link was found in
this page's content (live recon, 2026-08-20) - there is nothing to follow.

Live recon (2026-08-20) confirmed this is **not** the ``<h1>``/``<h2>``/
``<h3>`` prose-section shape ``app/crawlers/stpete_program_details.py``'s
``parse_program_detail_page()`` expects: it is a single dense HTML
``<table>`` (``<thead>`` + 8 ``<tbody>`` rows, one per household size 1-8)
inside a ``div.scroll-container``, itself inside the same
``#post .module-container`` region every other stpete.org detail page
uses. The table has 12 ``<th>`` columns: the first is a "Household Size"
row-label column (not a data column), the other 11 are AMI-tier columns.
Each tier column's header text is of the form ``N% AMI`` optionally
followed by one or more program names in parens (e.g. ``50% AMI (HOME/
SHIP/ CDBG-DR)``) - a single dollar amount can be shared by multiple
programs at that percent. One column (``100% AMI``) has no parenthetical
program name at all: it is the bare median-income benchmark, not tied to
a specific assistance program, so ``AMIThreshold.program`` is ``None`` for
that column's rows (nullable per ``.claude/rules/data.md``, not a
sentinel - it is genuinely not a program-specific figure).

A single ``<h2>`` immediately above the table states per-program effective
dates as free text (e.g. "SHIP Effective: May 1, 2026 / NSP Effective: May
1, 2025/ HOME Effective: June 1, 2025 / CDBG-DR Effective: June 1, 2026").
Live recon found 6 distinct program tokens across the table's column
headers - SHIP, HOME, NSP, CDBG-DR, TIF, WFH - but the ``<h2>`` line only
states a date for 4 of them (SHIP/HOME/NSP/CDBG-DR). TIF and WFH have no
stated effective date anywhere on the page: their ``AMIThreshold`` rows
carry ``published_date=None`` (nullable, not a sentinel), same discipline
as every other date-optional field in this codebase. This refines
DECISIONS #43's own recon note ("5 program tiers (SHIP/HOME/NSP/CDBG-DR)")
- the live page actually carries 6 named program tokens plus the one
unlabeled bare-``100% AMI`` column; see DECISIONS #44.

Because one dollar amount can be shared by several programs in a single
column, one ``AMIThreshold`` row is produced per (household_size, program)
pair rather than per (household_size, column) - a caller resolving, say,
``purchase_assistance_program.php``'s "80% AMI" for the HOME program needs
a HOME-specific row with HOME's own effective date attached, even where
the dollar figure happens to be shared with SHIP/CDBG-DR at the same
percent. The dollar amount is intentionally duplicated across each such
row rather than deduplicated - it is the same real-world number restated
once per program it applies to, not redundant data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

from bs4 import BeautifulSoup
from bs4.element import Tag

from app.crawlers.base import Attribution, BaseCrawler
from app.crawlers.stpete_grant_categories import SOUTH_STPETE_CONTENT_SELECTOR

# --- The 1 URL DECISIONS #43 names, exactly ---------------------------------

INCOME_LIMITS_URL = "https://www.stpete.org/residents/housing/income_limits.php"

# Matches a table column header like "50% AMI (HOME/ SHIP/ CDBG-DR)" or the
# bare "100% AMI" (no program group at all).
_COLUMN_HEADER_RE = re.compile(r"^(\d+)%\s*AMI(?:\s*\(([^)]+)\))?$")

# Matches one "PROGRAM Effective: Month D, YYYY" clause in the page's <h2>
# effective-dates line. Program tokens observed live: SHIP, NSP, HOME,
# CDBG-DR (hyphenated tokens like CDBG-DR are why "-" is allowed).
_EFFECTIVE_DATE_CLAUSE_RE = re.compile(r"([A-Z][A-Za-z-]*)\s+Effective:\s*([A-Za-z]+ \d{1,2},\s*\d{4})")


@dataclass(frozen=True)
class AMIThreshold:
    """One (household_size, program, ami_percent) cell of income_limits.php's
    table. ``program`` is ``None`` only for the one observed column with no
    program name in its header (the bare ``100% AMI`` median-income
    benchmark). ``dollar_amount`` is whole dollars - every value observed
    live is a round dollar figure, no cents."""

    household_size: int
    ami_percent: int
    program: str | None
    dollar_amount: int
    attribution: Attribution


class StpeteIncomeLimitsCrawler(BaseCrawler):
    """Crawls DECISIONS #43's income_limits.php AMI reference table. See
    module docstring."""

    def __init__(self, **kwargs) -> None:
        super().__init__(source_name="stpete_income_limits", **kwargs)

    def crawl(self) -> list[AMIThreshold]:
        resp = self.fetch(INCOME_LIMITS_URL)
        return self.parse_income_limits_page(resp.text, INCOME_LIMITS_URL)

    def parse_income_limits_page(self, html: str, page_url: str) -> list[AMIThreshold]:
        soup = BeautifulSoup(html, "lxml")

        container = soup.select_one(SOUTH_STPETE_CONTENT_SELECTOR)
        if container is None:
            self.fail_loud(
                f"content container {SOUTH_STPETE_CONTENT_SELECTOR!r} not found on {page_url} "
                "- stpete.org's income_limits.php page structure may have changed"
            )

        table = container.find("table")
        if table is None:
            self.fail_loud(f"no <table> found in {SOUTH_STPETE_CONTENT_SELECTOR!r} on {page_url}")

        thead = table.find("thead")
        tbody = table.find("tbody")
        if thead is None or tbody is None:
            self.fail_loud(f"income table on {page_url} has no <thead>/<tbody>")

        header_cells = thead.find_all("th")
        if len(header_cells) < 2:
            self.fail_loud(f"income table header on {page_url} has fewer than 2 columns")

        column_specs = self._parse_column_headers(header_cells, page_url)
        effective_dates = self._parse_effective_dates(container, page_url)

        retrieval_time = datetime.now(timezone.utc)

        rows = tbody.find_all("tr")
        if not rows:
            self.fail_loud(f"income table on {page_url} has a <tbody> with no rows")

        thresholds: list[AMIThreshold] = []
        for tr in rows:
            cells = tr.find_all("td")
            if len(cells) != len(header_cells):
                self.fail_loud(
                    f"income table row on {page_url} has {len(cells)} cells, "
                    f"expected {len(header_cells)} to match the header"
                )

            size_text = cells[0].get_text(strip=True)
            if not size_text.isdigit():
                self.fail_loud(
                    f"income table row's household-size cell {size_text!r} on {page_url} is not a whole number"
                )
            household_size = int(size_text)

            for (percent, programs), td in zip(column_specs, cells[1:]):
                amount = self._parse_dollar_amount(td.get_text(strip=True), page_url)
                for program in programs:
                    thresholds.append(
                        AMIThreshold(
                            household_size=household_size,
                            ami_percent=percent,
                            program=program,
                            dollar_amount=amount,
                            attribution=Attribution(
                                source_url=page_url,
                                retrieval_timestamp=retrieval_time,
                                # Nullable per .claude/rules/data.md - TIF
                                # and WFH have no stated effective date
                                # anywhere on the page (see module
                                # docstring), not a guess/sentinel.
                                published_date=effective_dates.get(program) if program else None,
                            ),
                        )
                    )

        if not thresholds:
            self.fail_loud(f"income table on {page_url} produced zero threshold rows")

        return thresholds

    def _parse_column_headers(
        self, header_cells: list[Tag], page_url: str
    ) -> list[tuple[int, list[str | None]]]:
        """Parses the 11 AMI-tier columns (all header cells after the first
        "Household Size" row-label column). Returns one (percent, programs)
        pair per column, where `programs` is `[None]` for the one observed
        column with no program name at all."""
        column_specs: list[tuple[int, list[str | None]]] = []
        for th in header_cells[1:]:
            text = th.get_text(" ", strip=True)
            match = _COLUMN_HEADER_RE.match(text)
            if not match:
                self.fail_loud(f"could not parse income table column header {text!r} on {page_url}")
            percent = int(match.group(1))
            programs_raw = match.group(2)
            programs: list[str | None] = (
                [p.strip() for p in programs_raw.split("/")] if programs_raw else [None]
            )
            column_specs.append((percent, programs))
        return column_specs

    def _parse_dollar_amount(self, text: str, page_url: str) -> int:
        cleaned = text.replace("$", "").replace(",", "").strip()
        if not cleaned.isdigit():
            self.fail_loud(f"could not parse dollar amount {text!r} on {page_url}")
        return int(cleaned)

    def _parse_effective_dates(self, container: Tag, page_url: str) -> dict[str, date]:
        h2 = container.find("h2")
        if h2 is None:
            self.fail_loud(f"no <h2> effective-dates line found on {page_url}")
        text = h2.get_text(" ", strip=True)
        matches = _EFFECTIVE_DATE_CLAUSE_RE.findall(text)
        if not matches:
            self.fail_loud(f"could not parse any 'PROGRAM Effective: DATE' clauses from {text!r} on {page_url}")

        dates: dict[str, date] = {}
        for program, date_text in matches:
            dates[program] = datetime.strptime(date_text.strip(), "%B %d, %Y").date()
        return dates
