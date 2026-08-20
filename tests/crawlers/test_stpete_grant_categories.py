"""Tests for app/crawlers/stpete_grant_categories.py - the 4 pure-hub
category sub-pages (business/community/housing/youth) named in
DECISIONS #30.

Fixtures recorded from the real live pages via this module's own
crawler's fetch() during this session - see DECISIONS #30-32. The
for_south_stpete.php and sunrise_st._pete/index.php fixtures are also
recorded here (same recording run) but tested separately in
test_stpete_grant_categories_south_stpete.py and
test_stpete_grant_categories_sunrise.py since those two pages use
different parsers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import responses

from app.crawlers.base import CrawlerStructureError
from app.crawlers.stpete_grant_categories import (
    BUSINESS_URL,
    COMMUNITY_URL,
    HOUSING_URL,
    HUB_PAGE_URLS,
    YOUTH_URL,
    StpeteGrantCategoryPagesCrawler,
)
from tests.conftest import register_robots_permissive

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "stpete_grant_categories"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_crawler() -> StpeteGrantCategoryPagesCrawler:
    return StpeteGrantCategoryPagesCrawler(min_request_interval_seconds=0)


# --- Real-fixture parsing: shape and attribution, per hub page -------------


@pytest.mark.parametrize(
    "fixture_name,url,expected_count,expected_names",
    [
        ("business.html", BUSINESS_URL, 3, {"For Business Owners", "For Developers", "For Property Owners"}),
        ("community.html", COMMUNITY_URL, 10, {"City of the Arts Grant", "Stormwater Utility Fee Credits"}),
        ("housing.html", HOUSING_URL, 9, {"Affordable Housing Lot Disposition Program", "Sunrise St. Pete"}),
        ("youth.html", YOUTH_URL, 4, {"Youth Development Grants", "Youth Opportunity Grants"}),
    ],
)
def test_parse_hub_page_real_fixture(fixture_name, url, expected_count, expected_names):
    html = load_fixture(fixture_name)
    crawler = make_crawler()
    tiles = crawler._tiles_parser.parse_tiles_page(html, url)

    assert len(tiles) == expected_count
    names = {t.category_name for t in tiles}
    assert expected_names <= names

    for t in tiles:
        assert t.category_name
        assert t.category_url.startswith("https://")
        # Mandatory attribution per .claude/rules/crawler.md.
        assert t.attribution.source_url == url
        assert t.attribution.retrieval_timestamp.tzinfo is not None
        # Confirmed live (DECISIONS #32): every tile's caption is empty on
        # all 4 hub pages - no per-item description at this directory
        # level, nullable per .claude/rules/data.md, not a sentinel.
        assert t.attribution.published_date is None


def test_hub_page_urls_match_decisions_30():
    assert set(HUB_PAGE_URLS.values()) == {BUSINESS_URL, COMMUNITY_URL, HOUSING_URL, YOUTH_URL}


# --- Fail-loud: structure-parsing failures (reuses stpete_grants's parser) -


def test_parse_hub_page_missing_container_raises():
    html = "<html><body><p>stpete.org redesigned this page</p></body></html>"
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="tiles container"):
        crawler._tiles_parser.parse_tiles_page(html, BUSINESS_URL)


# --- crawl_hub_pages() end-to-end against mocked HTTP -----------------------


@responses.activate
def test_crawl_hub_pages_fetches_and_parses_all_4():
    register_robots_permissive(responses, host="www.stpete.org")
    responses.add(responses.GET, BUSINESS_URL, body=load_fixture("business.html"), status=200)
    responses.add(responses.GET, COMMUNITY_URL, body=load_fixture("community.html"), status=200)
    responses.add(responses.GET, HOUSING_URL, body=load_fixture("housing.html"), status=200)
    responses.add(responses.GET, YOUTH_URL, body=load_fixture("youth.html"), status=200)

    crawler = make_crawler()
    results = crawler.crawl_hub_pages()

    assert set(results.keys()) == {BUSINESS_URL, COMMUNITY_URL, HOUSING_URL, YOUTH_URL}
    assert len(results[BUSINESS_URL]) == 3
    assert len(results[COMMUNITY_URL]) == 10
    assert len(results[HOUSING_URL]) == 9
    assert len(results[YOUTH_URL]) == 4
