"""Tests for app/crawlers/stpete_grants.py.

Uses a real fixture (tests/fixtures/stpete_grants/grants_index.html)
recorded from https://stpete.org/residents/grants___loans/index.php via
the real crawler's own fetch() during this session — see DECISIONS #26-28.
Structure-failure (fail-loud) cases use small hand-built synthetic HTML,
matching the pattern in test_legistar.py, since there's no live "broken"
page to record.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import responses

from app.crawlers.base import CrawlerStructureError
from app.crawlers.stpete_grants import GRANTS_URL, StpeteGrantsCrawler
from tests.conftest import register_robots_permissive

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "stpete_grants" / "grants_index.html"


def load_fixture() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


def make_crawler() -> StpeteGrantsCrawler:
    return StpeteGrantsCrawler(min_request_interval_seconds=0)


# --- Fail-loud: structure-parsing failures ---------------------------------


def test_parse_index_missing_container_raises():
    html = "<html><body><p>stpete.org redesigned this page</p></body></html>"
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="tiles container"):
        crawler.parse_index(html)


def test_parse_index_container_with_no_tiles_raises():
    html = '<html><body><div class="v2-tiles-con"></div></body></html>'
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="no div.v2-tile"):
        crawler.parse_index(html)


def test_parse_tile_missing_link_raises():
    html = """
    <html><body>
    <div class="v2-tiles-con">
      <div class="v2-tile"><div class="v2-tile-info"><p class="v2-tile-caption"></p></div></div>
    </div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="no a.v2-tile-link"):
        crawler.parse_index(html)


# --- Real-fixture parsing: shape and attribution ---------------------------


def test_parse_index_real_fixture():
    html = load_fixture()
    crawler = make_crawler()
    categories = crawler.parse_index(html)

    # Recon (DECISIONS #27) confirmed exactly 6 category tiles on the live
    # page — PLAN.md's "6 categories".
    assert len(categories) == 6

    names = {c.category_name for c in categories}
    assert "For Business" in names
    assert "Sunrise St. Pete" in names

    for c in categories:
        assert c.category_name
        assert c.category_url.startswith("https://")
        # Mandatory attribution per .claude/rules/crawler.md: source URL +
        # retrieval timestamp always present; published_date is legitimately
        # None here (no per-item date on this index page — DECISIONS #27).
        assert c.attribution.source_url == GRANTS_URL
        assert c.attribution.published_date is None
        assert isinstance(c.attribution.retrieval_timestamp, datetime)
        assert c.attribution.retrieval_timestamp.tzinfo is not None


def test_parse_index_resolves_links_against_declared_base_tag():
    """DECISIONS #28: the page declares <base href="https://www.stpete.org/" />
    and tile hrefs are site-root-relative. Resolving against GRANTS_URL's
    own directory instead would silently double the path — this asserts
    the real fixture's business tile resolves to the correct, non-doubled
    URL."""
    html = load_fixture()
    crawler = make_crawler()
    categories = crawler.parse_index(html)

    business = next(c for c in categories if c.category_name == "For Business")
    assert business.category_url == "https://www.stpete.org/residents/grants___loans/business.php"
    assert "grants___loans/residents" not in business.category_url


# --- crawl() end-to-end against mocked HTTP ---------------------------------


@responses.activate
def test_crawl_fetches_and_parses():
    register_robots_permissive(responses, host="stpete.org")
    responses.add(responses.GET, GRANTS_URL, body=load_fixture(), status=200)

    crawler = make_crawler()
    categories = crawler.crawl()
    assert len(categories) == 6
