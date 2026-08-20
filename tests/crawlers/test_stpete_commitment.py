"""Tests for app/crawlers/stpete_commitment.py - DECISIONS #46's
st_petes_commitment.php ACCC action-item work plan.

Fixture recorded from the real live page via this module's own crawler's
fetch() during this session - see DECISIONS #46/#47.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import responses

from app.crawlers.base import CrawlerStructureError
from app.crawlers.stpete_commitment import (
    COMMITMENT_URL,
    StpeteCommitmentActionItem,
    StpeteCommitmentCrawler,
)
from tests.conftest import register_robots_permissive

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "stpete_commitment"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_crawler() -> StpeteCommitmentCrawler:
    return StpeteCommitmentCrawler(min_request_interval_seconds=0)


# --- Real-fixture parsing ----------------------------------------------------


def test_parse_commitment_page_real_fixture_shape():
    html = load_fixture("st_petes_commitment.html")
    crawler = make_crawler()
    items = crawler.parse_commitment_page(html, COMMITMENT_URL)

    assert len(items) == 16
    assert all(isinstance(i, StpeteCommitmentActionItem) for i in items)

    sectors = {i.sector for i in items}
    assert sectors == {"Building Sector", "Transportation Sector"}

    building = [i for i in items if i.sector == "Building Sector"]
    transportation = [i for i in items if i.sector == "Transportation Sector"]
    assert len(building) == 10
    assert len(transportation) == 6

    for i in items:
        assert i.text
        assert i.tier_code in {"F", "A", "M"}
        assert i.tier_label
        # No per-item date exists on the page - see module docstring.
        # Nullable per .claude/rules/data.md, not a sentinel.
        assert i.attribution.published_date is None
        # Mandatory attribution per .claude/rules/crawler.md.
        assert i.attribution.source_url == COMMITMENT_URL
        assert i.attribution.retrieval_timestamp.tzinfo is not None


def test_commitment_tier_label_mapping():
    html = load_fixture("st_petes_commitment.html")
    crawler = make_crawler()
    items = crawler.parse_commitment_page(html, COMMITMENT_URL)

    by_tier = {i.tier_code: i.tier_label for i in items}
    assert by_tier["F"] == "Foundational"
    assert by_tier["A"] == "Ambitious"
    assert by_tier["M"] == "Moonshot"

    # Exactly 1 Moonshot item on the page - the Duke Energy one.
    moonshots = [i for i in items if i.tier_code == "M"]
    assert len(moonshots) == 1


def test_commitment_duke_energy_moonshot_item():
    html = load_fixture("st_petes_commitment.html")
    crawler = make_crawler()
    items = crawler.parse_commitment_page(html, COMMITMENT_URL)

    duke = next(i for i in items if "Duke Energy" in i.text)
    assert duke.sector == "Building Sector"
    assert duke.tier_code == "M"
    assert duke.tier_label == "Moonshot"
    assert "energy equity" in duke.text
    assert "low income area" in duke.text
    assert duke.links == ()


def test_commitment_off_host_links_preserved_verbatim_not_fetched():
    html = load_fixture("st_petes_commitment.html")
    crawler = make_crawler()
    items = crawler.parse_commitment_page(html, COMMITMENT_URL)

    solar_coop = next(i for i in items if "residential solar co-op" in i.text)
    assert solar_coop.links == (
        "https://www.solarunitedneighbors.org/florida/go-solar-in-florida/go-solar-in-a-florida-group/",
    )

    self_fund = next(i for i in items if "Solar and Energy Loan Fund" in i.text)
    assert self_fund.links == ("https://solarenergyloanfund.org/",)


def test_commitment_relative_link_resolved_on_host():
    html = load_fixture("st_petes_commitment.html")
    crawler = make_crawler()
    items = crawler.parse_commitment_page(html, COMMITMENT_URL)

    mobility = next(i for i in items if "new mobility options" in i.text)
    assert mobility.links == ("https://www.stpete.org/visitors/scooter_safety.php",)


# --- Fail-loud: structure-parsing failures ----------------------------------


def test_parse_missing_content_containers_raises():
    html = "<html><body><p>stpete.org redesigned this page</p></body></html>"
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="content blocks"):
        crawler.parse_commitment_page(html, COMMITMENT_URL)


def test_parse_no_sector_headings_raises():
    html = """
    <html><body>
    <div id="post"><div class="module-container">
        <h3>Some Other Heading</h3>
        <p>No sector headings here.</p>
    </div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="sector heading"):
        crawler.parse_commitment_page(html, COMMITMENT_URL)


def test_parse_sector_with_no_ul_raises():
    html = """
    <html><body>
    <div id="post"><div class="module-container">
        <h4>Building Sector</h4>
        <p>No list follows this heading.</p>
    </div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="no <ul> of action items"):
        crawler.parse_commitment_page(html, COMMITMENT_URL)


def test_parse_sector_with_empty_ul_raises():
    html = """
    <html><body>
    <div id="post"><div class="module-container">
        <h4>Building Sector</h4>
        <ul></ul>
    </div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="empty action-item"):
        crawler.parse_commitment_page(html, COMMITMENT_URL)


def test_parse_action_item_missing_tier_marker_raises():
    html = """
    <html><body>
    <div id="post"><div class="module-container">
        <h4>Building Sector</h4>
        <ul><li>An action item with no trailing tier marker</li></ul>
    </div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="tier marker"):
        crawler.parse_commitment_page(html, COMMITMENT_URL)


def test_parse_missing_duke_energy_item_raises():
    """A page whose structure otherwise parses fine but that no longer
    contains the item DECISIONS #46 was added for is a real content-drift
    signal, not just a cosmetic markup change - fails loud rather than
    silently returning a technically-valid-but-wrong result."""
    html = """
    <html><body>
    <div id="post"><div class="module-container">
        <h4>Building Sector</h4>
        <ul><li>Some unrelated action item (F)</li></ul>
    </div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="Duke Energy community solar"):
        crawler.parse_commitment_page(html, COMMITMENT_URL)


# --- crawl() end-to-end against mocked HTTP ---------------------------------


@responses.activate
def test_crawl_fetches_and_parses():
    register_robots_permissive(responses, host="www.stpete.org")
    responses.add(responses.GET, COMMITMENT_URL, body=load_fixture("st_petes_commitment.html"), status=200)

    crawler = make_crawler()
    items = crawler.crawl()

    assert len(items) == 16
    assert all(i.attribution.source_url == COMMITMENT_URL for i in items)
