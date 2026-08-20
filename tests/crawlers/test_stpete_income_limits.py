"""Tests for app/crawlers/stpete_income_limits.py - DECISIONS #43's
income_limits.php AMI reference table.

Fixture recorded from the real live page via this module's own crawler's
fetch() during this session - see DECISIONS #43-44.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import responses

from app.crawlers.base import CrawlerStructureError
from app.crawlers.stpete_income_limits import (
    INCOME_LIMITS_URL,
    AMIThreshold,
    StpeteIncomeLimitsCrawler,
)
from tests.conftest import register_robots_permissive

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "stpete_income_limits"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_crawler() -> StpeteIncomeLimitsCrawler:
    return StpeteIncomeLimitsCrawler(min_request_interval_seconds=0)


# --- Real-fixture parsing ----------------------------------------------------


def test_parse_income_limits_real_fixture_shape():
    html = load_fixture("income_limits.html")
    crawler = make_crawler()
    thresholds = crawler.parse_income_limits_page(html, INCOME_LIMITS_URL)

    # 8 household sizes x 15 program-columns (some AMI-percent columns list
    # more than 1 program sharing a dollar amount - see module docstring).
    assert len(thresholds) == 120
    assert all(isinstance(t, AMIThreshold) for t in thresholds)

    sizes = {t.household_size for t in thresholds}
    assert sizes == {1, 2, 3, 4, 5, 6, 7, 8}

    percents = {t.ami_percent for t in thresholds}
    assert percents == {30, 50, 60, 80, 100, 120, 140, 150}

    programs = {t.program for t in thresholds}
    assert programs == {"SHIP", "HOME", "NSP", "CDBG-DR", "TIF", "WFH", None}

    for t in thresholds:
        assert t.dollar_amount > 0
        # Mandatory attribution per .claude/rules/crawler.md.
        assert t.attribution.source_url == INCOME_LIMITS_URL
        assert t.attribution.retrieval_timestamp.tzinfo is not None


def test_income_limits_real_dollar_range():
    html = load_fixture("income_limits.html")
    crawler = make_crawler()
    thresholds = crawler.parse_income_limits_page(html, INCOME_LIMITS_URL)

    amounts = [t.dollar_amount for t in thresholds]
    assert min(amounts) == 21950  # household size 1, 30% AMI (HOME)
    assert max(amounts) == 227100  # household size 8, 150% AMI (WFH)


def test_income_limits_per_program_effective_dates():
    html = load_fixture("income_limits.html")
    crawler = make_crawler()
    thresholds = crawler.parse_income_limits_page(html, INCOME_LIMITS_URL)

    by_program_date = {t.program: t.attribution.published_date for t in thresholds}
    assert by_program_date["SHIP"] == date(2026, 5, 1)
    assert by_program_date["NSP"] == date(2025, 5, 1)
    assert by_program_date["HOME"] == date(2025, 6, 1)
    assert by_program_date["CDBG-DR"] == date(2026, 6, 1)

    # TIF and WFH have no stated effective date on the page - nullable per
    # .claude/rules/data.md, not a guessed sentinel.
    assert by_program_date["TIF"] is None
    assert by_program_date["WFH"] is None
    # The bare "100% AMI" column has no program at all - also nullable.
    assert by_program_date[None] is None


def test_income_limits_shared_dollar_amount_across_programs_in_one_column():
    """50% AMI (HOME/ SHIP/ CDBG-DR) is one column sharing one dollar
    figure across 3 programs - each program still gets its own row with
    its own effective date, per module docstring."""
    html = load_fixture("income_limits.html")
    crawler = make_crawler()
    thresholds = crawler.parse_income_limits_page(html, INCOME_LIMITS_URL)

    size_1_50pct = [t for t in thresholds if t.household_size == 1 and t.ami_percent == 50]
    programs = {t.program for t in size_1_50pct}
    assert programs == {"HOME", "SHIP", "CDBG-DR"}
    assert all(t.dollar_amount == 40150 for t in size_1_50pct)
    dates = {t.program: t.attribution.published_date for t in size_1_50pct}
    assert dates == {
        "HOME": date(2025, 6, 1),
        "SHIP": date(2026, 5, 1),
        "CDBG-DR": date(2026, 6, 1),
    }


# --- Fail-loud: structure-parsing failures ----------------------------------


def test_parse_missing_content_container_raises():
    html = "<html><body><p>stpete.org redesigned this page</p></body></html>"
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="content container"):
        crawler.parse_income_limits_page(html, INCOME_LIMITS_URL)


def test_parse_missing_table_raises():
    html = """
    <html><body>
    <div id="post"><div class="module-container">
        <h1>Fiscal Year 2026 Income Limits</h1>
        <h2>SHIP Effective: May 1, 2026</h2>
        <p>stpete.org replaced the table with something else</p>
    </div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="no <table>"):
        crawler.parse_income_limits_page(html, INCOME_LIMITS_URL)


def test_parse_unrecognized_column_header_raises():
    html = """
    <html><body>
    <div id="post"><div class="module-container">
        <h1>Fiscal Year 2026 Income Limits</h1>
        <h2>SHIP Effective: May 1, 2026</h2>
        <table>
            <thead><tr><th>Household Size</th><th>Not An AMI Column</th></tr></thead>
            <tbody><tr><td>1</td><td>$1,000</td></tr></tbody>
        </table>
    </div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="could not parse income table column header"):
        crawler.parse_income_limits_page(html, INCOME_LIMITS_URL)


def test_parse_missing_effective_dates_h2_raises():
    html = """
    <html><body>
    <div id="post"><div class="module-container">
        <h1>Fiscal Year 2026 Income Limits</h1>
        <table>
            <thead><tr><th>Household Size</th><th>30% AMI (SHIP)</th></tr></thead>
            <tbody><tr><td>1</td><td>$1,000</td></tr></tbody>
        </table>
    </div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="no <h2> effective-dates line"):
        crawler.parse_income_limits_page(html, INCOME_LIMITS_URL)


def test_parse_malformed_dollar_amount_raises():
    html = """
    <html><body>
    <div id="post"><div class="module-container">
        <h1>Fiscal Year 2026 Income Limits</h1>
        <h2>SHIP Effective: May 1, 2026</h2>
        <table>
            <thead><tr><th>Household Size</th><th>30% AMI (SHIP)</th></tr></thead>
            <tbody><tr><td>1</td><td>not a dollar figure</td></tr></tbody>
        </table>
    </div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="could not parse dollar amount"):
        crawler.parse_income_limits_page(html, INCOME_LIMITS_URL)


def test_parse_row_column_count_mismatch_raises():
    html = """
    <html><body>
    <div id="post"><div class="module-container">
        <h1>Fiscal Year 2026 Income Limits</h1>
        <h2>SHIP Effective: May 1, 2026</h2>
        <table>
            <thead><tr><th>Household Size</th><th>30% AMI (SHIP)</th><th>50% AMI (HOME)</th></tr></thead>
            <tbody><tr><td>1</td><td>$1,000</td></tr></tbody>
        </table>
    </div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="expected 3 to match the header"):
        crawler.parse_income_limits_page(html, INCOME_LIMITS_URL)


# --- crawl() end-to-end against mocked HTTP ---------------------------------


@responses.activate
def test_crawl_fetches_and_parses():
    register_robots_permissive(responses, host="www.stpete.org")
    responses.add(responses.GET, INCOME_LIMITS_URL, body=load_fixture("income_limits.html"), status=200)

    crawler = make_crawler()
    thresholds = crawler.crawl()

    assert len(thresholds) == 120
    assert all(t.attribution.source_url == INCOME_LIMITS_URL for t in thresholds)
