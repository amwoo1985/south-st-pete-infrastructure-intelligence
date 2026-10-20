"""Tests for app/crawlers/pinellas_cf.py.

Uses a real fixture (tests/fixtures/pinellas_cf/grants_index.html)
recorded from https://pinellascf.org/nonprofits/grants/ via the real
crawler's own fetch() during this session — see DECISIONS #29.
Structure-failure (fail-loud) cases use small hand-built synthetic HTML,
matching the pattern in test_legistar.py / test_stpete_grants.py.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
import responses

from app.crawlers.base import CrawlerStructureError
from app.crawlers.pinellas_cf import GRANTS_URL, PinellasCFCrawler, _parse_award_date
from tests.conftest import register_robots_permissive

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "pinellas_cf" / "grants_index.html"


def load_fixture() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


def make_crawler() -> PinellasCFCrawler:
    return PinellasCFCrawler(min_request_interval_seconds=0)


# --- _parse_award_date ------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("March 2026", date(2026, 3, 1)),
        ("Early December 2026", date(2026, 12, 1)),
        ("Application window dates announced in January 2027 (Invitation Only)", date(2027, 1, 1)),
        ("TBD", None),
        ("Application Window Closed (Invitation Only)", None),
    ],
)
def test_parse_award_date(text, expected):
    assert _parse_award_date(text) == expected


# --- Fail-loud: structure-parsing failures ---------------------------------


def test_parse_grants_table_missing_table_raises():
    html = "<html><body><p>Pinellas CF redesigned this page</p></body></html>"
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="table not found"):
        crawler.parse_grants_table(html)


def test_parse_grants_table_missing_headers_raises():
    html = """
    <html><body>
    <div class="table-2"><table>
      <thead><tr><th>Program</th><th>When</th></tr></thead>
      <tbody><tr><td>x</td></tr></tbody>
    </table></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="missing expected column"):
        crawler.parse_grants_table(html)


def test_parse_grants_table_no_tbody_raises():
    html = """
    <html><body>
    <div class="table-2"><table>
      <thead><tr><th>GRANT PROGRAM</th><th>APPLICATION TIMELINE</th>
      <th>AWARD DISTRIBUTION</th><th>MORE INFO</th></tr></thead>
    </table></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="no <tbody>"):
        crawler.parse_grants_table(html)


def test_parse_row_wrong_cell_count_raises():
    html = """
    <html><body>
    <div class="table-2"><table>
      <thead><tr><th>GRANT PROGRAM</th><th>APPLICATION TIMELINE</th>
      <th>AWARD DISTRIBUTION</th><th>MORE INFO</th></tr></thead>
      <tbody><tr><td>Only One Cell</td></tr></tbody>
    </table></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="expected 4"):
        crawler.parse_grants_table(html)


# --- Real-fixture parsing: shape and attribution ---------------------------


def test_parse_grants_table_real_fixture():
    html = load_fixture()
    crawler = make_crawler()
    programs = crawler.parse_grants_table(html)

    # Recon (DECISIONS #29) confirmed 7 rows across 5 distinct base program
    # names — PLAN.md's "5 programs + dated 2026 table". Senior Citizens
    # Services Grants accounts for 3 of the 7 rows (one per funding cycle:
    # Housing/Wellness/Support), each a distinct literal program_name.
    assert len(programs) == 7
    base_names = {p.program_name.split(":")[0].strip() for p in programs}
    assert len(base_names) == 5

    names = {p.program_name for p in programs}
    assert "Huntley Arts Enrichment for Youth Grant" in names
    assert "Senior Citizens Services Grants: Housing" in names

    for p in programs:
        assert p.program_name
        assert p.detail_url.startswith("https://pinellascf.org/")
        # Mandatory attribution: source URL + retrieval timestamp always
        # present. published_date is populated for every observed row
        # (DECISIONS #29's regex matched all 7 live), but the field stays
        # Optional for a future unannounced-date row.
        assert p.attribution.source_url == GRANTS_URL
        assert isinstance(p.attribution.retrieval_timestamp, datetime)
        assert p.attribution.retrieval_timestamp.tzinfo is not None

    housing = next(p for p in programs if p.program_name == "Senior Citizens Services Grants: Housing")
    assert housing.attribution.published_date == date(2026, 3, 1)


# --- crawl() end-to-end against mocked HTTP ---------------------------------


@responses.activate
def test_crawl_fetches_and_parses():
    register_robots_permissive(responses, host="pinellascf.org")
    responses.add(responses.GET, GRANTS_URL, body=load_fixture(), status=200)

    crawler = make_crawler()
    programs = crawler.crawl()
    assert len(programs) == 7
