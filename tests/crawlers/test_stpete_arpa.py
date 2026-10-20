"""Tests for app/crawlers/stpete_arpa.py - DECISIONS #43's
american_rescue_plan_act.php funding breakdown.

Fixture recorded from the real live page via this module's own crawler's
fetch() during this session - see DECISIONS #43/#45.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import responses

from app.crawlers.base import CrawlerStructureError
from app.crawlers.stpete_arpa import ARPA_URL, ArpaFundingAllocation, StpeteArpaCrawler
from tests.conftest import register_robots_permissive

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "stpete_arpa"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_crawler() -> StpeteArpaCrawler:
    return StpeteArpaCrawler(min_request_interval_seconds=0)


# --- Real-fixture parsing ----------------------------------------------------


def test_parse_arpa_page_real_fixture_shape():
    html = load_fixture("american_rescue_plan_act.html")
    crawler = make_crawler()
    allocations = crawler.parse_arpa_page(html, ARPA_URL)

    assert len(allocations) == 10
    assert all(isinstance(a, ArpaFundingAllocation) for a in allocations)

    categories = {a.category for a in allocations}
    assert categories == {"Housing by the #s", "Health & Social Equity by the #s"}

    housing = [a for a in allocations if a.category == "Housing by the #s"]
    health = [a for a in allocations if a.category == "Health & Social Equity by the #s"]
    assert len(housing) == 6
    assert len(health) == 4

    for a in allocations:
        assert a.amount_text
        assert a.amount_dollars > 0
        assert a.description
        # No clean per-allocation date exists on the page - see module
        # docstring. Nullable per .claude/rules/data.md, not a sentinel.
        assert a.attribution.published_date is None
        # Mandatory attribution per .claude/rules/crawler.md.
        assert a.attribution.source_url == ARPA_URL
        assert a.attribution.retrieval_timestamp.tzinfo is not None


def test_arpa_total_confirms_against_45_million_headline():
    html = load_fixture("american_rescue_plan_act.html")
    crawler = make_crawler()
    allocations = crawler.parse_arpa_page(html, ARPA_URL)

    total = sum(a.amount_dollars for a in allocations)
    # The page's own headline figure is "approximately $45 million" - the
    # 10 named allocations sum to slightly over that, consistent with
    # "approximately". See module docstring / DECISIONS #45.
    assert 44_000_000 < total < 46_000_000
    assert total == 45_410_435


def test_arpa_amount_shorthand_and_plain_forms_both_parsed():
    html = load_fixture("american_rescue_plan_act.html")
    crawler = make_crawler()
    allocations = crawler.parse_arpa_page(html, ARPA_URL)

    by_amount_text = {a.amount_text: a.amount_dollars for a in allocations}
    assert by_amount_text["$6.5 million"] == 6_500_000
    assert by_amount_text["$23.8 million"] == 23_800_000
    assert by_amount_text["$1 million"] == 1_000_000
    assert by_amount_text["$160,000"] == 160_000
    assert by_amount_text["$946,435"] == 946_435


def test_arpa_deuces_allocation_links_and_description():
    html = load_fixture("american_rescue_plan_act.html")
    crawler = make_crawler()
    allocations = crawler.parse_arpa_page(html, ARPA_URL)

    deuces = next(a for a in allocations if "Deuces" in a.description)
    assert deuces.amount_text == "$6.5 million"
    assert "80% of the Area Median Income" in deuces.description
    assert deuces.links == ("https://www.stpete.org/residents/current_projects/deuces_rising.php",)


def test_arpa_off_host_link_preserved_verbatim_not_fetched():
    """The Summer Food Program link resolves off-host (stpeteparksrec.org)
    - captured verbatim in `links`, never fetched (hard stop, same as
    every other off-scope link found in this crawl chain)."""
    html = load_fixture("american_rescue_plan_act.html")
    crawler = make_crawler()
    allocations = crawler.parse_arpa_page(html, ARPA_URL)

    food_security = next(a for a in allocations if "Food Security" in a.description)
    assert "https://stpeteparksrec.org/summerfood/" in food_security.links


# --- Fail-loud: structure-parsing failures ----------------------------------


def test_parse_missing_content_containers_raises():
    html = "<html><body><p>stpete.org redesigned this page</p></body></html>"
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="content blocks"):
        crawler.parse_arpa_page(html, ARPA_URL)


def test_parse_no_category_headings_raises():
    html = """
    <html><body>
    <div id="post"><div class="module-container">
        <h2>Some Other Heading</h2>
        <p><strong>$1 million</strong> for something unrelated.</p>
    </div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="category headings"):
        crawler.parse_arpa_page(html, ARPA_URL)


def test_parse_category_with_no_paragraphs_raises():
    html = """
    <html><body>
    <div id="post"><div class="module-container">
        <div class="col-md-6">
            <h2>Housing by the #s</h2>
        </div>
    </div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="no <p> allocation entries"):
        crawler.parse_arpa_page(html, ARPA_URL)


def test_parse_allocation_missing_strong_raises():
    html = """
    <html><body>
    <div id="post"><div class="module-container">
        <div class="col-md-6">
            <h2>Housing by the #s</h2>
            <p>No bolded amount here at all.</p>
        </div>
    </div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="no <strong> amount"):
        crawler.parse_arpa_page(html, ARPA_URL)


def test_parse_allocation_malformed_amount_raises():
    html = """
    <html><body>
    <div id="post"><div class="module-container">
        <div class="col-md-6">
            <h2>Housing by the #s</h2>
            <p><strong>a lot of money</strong> for something.</p>
        </div>
    </div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="could not parse dollar amount"):
        crawler.parse_arpa_page(html, ARPA_URL)


def test_parse_allocation_no_description_raises():
    html = """
    <html><body>
    <div id="post"><div class="module-container">
        <div class="col-md-6">
            <h2>Housing by the #s</h2>
            <p><strong>$1 million</strong></p>
        </div>
    </div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="has no description"):
        crawler.parse_arpa_page(html, ARPA_URL)


# --- crawl() end-to-end against mocked HTTP ---------------------------------


@responses.activate
def test_crawl_fetches_and_parses():
    register_robots_permissive(responses, host="www.stpete.org")
    responses.add(responses.GET, ARPA_URL, body=load_fixture("american_rescue_plan_act.html"), status=200)

    crawler = make_crawler()
    allocations = crawler.crawl()

    assert len(allocations) == 10
    assert all(a.attribution.source_url == ARPA_URL for a in allocations)
