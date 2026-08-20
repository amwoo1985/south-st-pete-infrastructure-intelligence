"""Tests for app/crawlers/stpete_grant_categories.py's for_south_stpete.php
parser - the one page of the 6 DECISIONS #30 URLs with real freeform
per-program content directly on the page (no tile grid). See DECISIONS #33.

Uses a real fixture (tests/fixtures/stpete_grant_categories/for_south_stpete.html)
recorded from https://www.stpete.org/residents/grants___loans/for_south_stpete.php
via the real crawler's own fetch() during this session.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import responses

from app.crawlers.base import CrawlerStructureError
from app.crawlers.stpete_grant_categories import SOUTH_STPETE_URL, StpeteGrantCategoryPagesCrawler
from tests.conftest import register_robots_permissive

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "stpete_grant_categories" / "for_south_stpete.html"


def load_fixture() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


def make_crawler() -> StpeteGrantCategoryPagesCrawler:
    return StpeteGrantCategoryPagesCrawler(min_request_interval_seconds=0)


# --- Fail-loud: structure-parsing failures ---------------------------------


def test_parse_missing_content_container_raises():
    html = "<html><body><p>stpete.org redesigned this page</p></body></html>"
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="content container"):
        crawler.parse_south_stpete_page(html)


def test_parse_content_container_missing_h1_raises():
    html = """
    <html><body>
    <div id="post"><div class="module-container"><h2>Overview</h2></div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="no <h1>"):
        crawler.parse_south_stpete_page(html)


def test_parse_no_program_headings_raises():
    html = """
    <html><body>
    <div id="post"><div class="module-container">
        <h1>South St. Pete Housing and Neighborhoods</h1>
        <h2>Overview</h2>
        <p>Intro text with no program headings at all.</p>
    </div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="no <h3>/<h4> program headings"):
        crawler.parse_south_stpete_page(html)


# --- Real-fixture parsing: shape and attribution ----------------------------


def test_parse_south_stpete_page_real_fixture():
    html = load_fixture()
    crawler = make_crawler()
    programs = crawler.parse_south_stpete_page(html)

    # Recon confirmed 7 distinct program headings: 5 <h3> plus 2 nested
    # <h4> sub-programs ("Rapid Roof Replacement Program" under the Facade
    # Improvement Grant block, "CRA Developer Incentive Program" under the
    # Affordable Housing Redevelopment Loan block) - see module docstring /
    # DECISIONS #33.
    assert len(programs) == 7

    names = {p.program_name for p in programs}
    assert "Rebates for Affordable Residential Rehabs" in names
    assert "Rapid Roof Replacement Program" in names
    assert "CRA Developer Incentive Program" in names

    for p in programs:
        assert p.program_name
        # Mandatory attribution per .claude/rules/crawler.md.
        assert p.attribution.source_url == SOUTH_STPETE_URL
        assert p.attribution.retrieval_timestamp.tzinfo is not None
        # No per-program date appears in this page's plain text (confirmed
        # live) - nullable per .claude/rules/data.md, not a sentinel.
        assert p.attribution.published_date is None
        for url in p.detail_urls:
            assert url.startswith("http")

    rehab = next(p for p in programs if p.program_name == "Rebates for Affordable Residential Rehabs")
    assert rehab.description is not None
    assert "citywide" in rehab.description
    assert rehab.detail_urls == (
        "https://www.stpete.org/residents/grants___loans/rebates_for_affordable_residential_rehabs.php",
    )

    # A block whose only content is an application link (no description
    # prose) legitimately has description=None - nullable, not a failure.
    incentive = next(p for p in programs if p.program_name == "CRA Developer Incentive Program")
    assert incentive.description is None
    assert len(incentive.detail_urls) == 1


# --- crawl_south_stpete_page() end-to-end against mocked HTTP ---------------


@responses.activate
def test_crawl_south_stpete_page_fetches_and_parses():
    register_robots_permissive(responses, host="www.stpete.org")
    responses.add(responses.GET, SOUTH_STPETE_URL, body=load_fixture(), status=200)

    crawler = make_crawler()
    programs = crawler.crawl_south_stpete_page()
    assert len(programs) == 7
