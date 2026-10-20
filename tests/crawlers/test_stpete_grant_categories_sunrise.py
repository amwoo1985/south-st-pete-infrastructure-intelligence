"""Tests for app/crawlers/stpete_grant_categories.py's
sunrise_st._pete/index.php parser - the one page of the 6 DECISIONS #30
URLs whose tile grid carries real per-program detail (eligibility +
description) directly in the tile markup. See DECISIONS #34.

Uses a real fixture (tests/fixtures/stpete_grant_categories/sunrise_index.html)
recorded from https://www.stpete.org/residents/grants___loans/sunrise_st._pete/index.php
via the real crawler's own fetch() during this session.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import responses

from app.crawlers.base import CrawlerStructureError
from app.crawlers.stpete_grant_categories import SUNRISE_URL, StpeteGrantCategoryPagesCrawler
from tests.conftest import register_robots_permissive

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "stpete_grant_categories" / "sunrise_index.html"


def load_fixture() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


def make_crawler() -> StpeteGrantCategoryPagesCrawler:
    return StpeteGrantCategoryPagesCrawler(min_request_interval_seconds=0)


# --- Fail-loud: structure-parsing failures ---------------------------------


def test_parse_sunrise_missing_container_raises():
    html = "<html><body><p>stpete.org redesigned this page</p></body></html>"
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="tiles container"):
        crawler.parse_sunrise_page(html)


def test_parse_sunrise_container_with_no_tiles_raises():
    html = '<html><body><div class="v2-tiles-con"></div></body></html>'
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="no div.v2-tile"):
        crawler.parse_sunrise_page(html)


def test_parse_sunrise_tile_missing_info_div_raises():
    html = """
    <html><body>
    <div class="v2-tiles-con"><div class="v2-tile"></div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="no div.v2-tile-info"):
        crawler.parse_sunrise_page(html)


def test_parse_sunrise_tile_missing_link_raises():
    html = """
    <html><body>
    <div class="v2-tiles-con">
      <div class="v2-tile"><div class="v2-tile-info"><p class="v2-tile-caption"></p></div></div>
    </div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="no a.v2-tile-link"):
        crawler.parse_sunrise_page(html)


# --- Real-fixture parsing: shape and attribution ----------------------------


def test_parse_sunrise_page_real_fixture():
    html = load_fixture()
    crawler = make_crawler()
    programs = crawler.parse_sunrise_page(html)

    # Recon confirmed exactly 5 "Active Programs" tiles.
    assert len(programs) == 5

    names = {p.program_name for p in programs}
    assert "Affordable Rental Housing Program" in names
    assert "Voluntary Buyout Program" in names

    for p in programs:
        assert p.program_name
        assert p.program_url.startswith("https://www.stpete.org/")
        # Every tile observed carries both an eligibility line and a
        # description paragraph beyond the empty caption.
        assert p.eligibility
        assert p.description
        # Mandatory attribution per .claude/rules/crawler.md.
        assert p.attribution.source_url == SUNRISE_URL
        assert p.attribution.retrieval_timestamp.tzinfo is not None
        # No per-program date appears in any tile's text (confirmed live) -
        # nullable per .claude/rules/data.md, not a sentinel.
        assert p.attribution.published_date is None

    arhp = next(p for p in programs if p.program_name == "Affordable Rental Housing Program")
    assert arhp.eligibility == "For developers and landlords of 10+ units"
    assert "Request for Applications (RFA)" in arhp.description
    assert arhp.program_url == "https://www.stpete.org/residents/grants___loans/sunrise_st._pete/arhp.php"

    # Non-.php CMS short-link tile hrefs (e.g. "HAP", "sunrise/VBP") still
    # resolve correctly against the declared <base> - same DECISIONS #28
    # mechanism as every other tile link on this site.
    hap = next(p for p in programs if p.program_name == "Homebuyer Assistance Program")
    assert hap.program_url == "https://www.stpete.org/HAP"


# --- crawl_sunrise_page() end-to-end against mocked HTTP --------------------


@responses.activate
def test_crawl_sunrise_page_fetches_and_parses():
    register_robots_permissive(responses, host="www.stpete.org")
    responses.add(responses.GET, SUNRISE_URL, body=load_fixture(), status=200)

    crawler = make_crawler()
    programs = crawler.crawl_sunrise_page()
    assert len(programs) == 5
