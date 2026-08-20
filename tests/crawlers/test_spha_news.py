"""Tests for app/crawlers/spha_news.py - DECISIONS #48/#51's
`www.stpeteha.org/news` index.

Fixture recorded from the real live page via this module's own crawler's
fetch() during this session - see DECISIONS #48/#51. Same real (not 404)
robots.txt note as tests/crawlers/test_spha_program_pages.py.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import responses

from app.crawlers.base import CrawlerStructureError
from app.crawlers.spha_news import NEWS_URL, SphaNewsCrawler, SphaNewsIndexItem

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "spha_news"

_ROBOTS_BODY = "User-agent: *\nDisallow: \nSitemap: https://www.stpeteha.org/sitemap.xml\n"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_crawler() -> SphaNewsCrawler:
    return SphaNewsCrawler(min_request_interval_seconds=0)


def register_spha_robots(responses_mock) -> None:
    responses_mock.add(
        responses_mock.GET,
        "https://www.stpeteha.org/robots.txt",
        body=_ROBOTS_BODY,
        status=200,
    )


# --- Real-fixture parsing ----------------------------------------------------


def test_parse_news_index_real_fixture_shape():
    html = load_fixture("news.html")
    items = make_crawler().parse_news_index(html, NEWS_URL)

    assert len(items) == 20
    assert all(isinstance(i, SphaNewsIndexItem) for i in items)
    for item in items:
        assert isinstance(item.published_date, date)
        assert item.title
        assert item.item_url.startswith("https://www.stpeteha.org/news-view?id=")
        # Mandatory attribution per .claude/rules/crawler.md.
        assert item.attribution.source_url == NEWS_URL
        assert item.attribution.retrieval_timestamp.tzinfo is not None
        # Unlike every other DECISIONS #48 page, this index has a real
        # per-item date - not nullable here. See module docstring.
        assert item.attribution.published_date == item.published_date


def test_first_item_content():
    html = load_fixture("news.html")
    items = make_crawler().parse_news_index(html, NEWS_URL)

    first = items[0]
    assert first.published_date == date(2026, 8, 20)
    assert first.item_url == "https://www.stpeteha.org/news-view?id=688"
    assert "Regular Meeting of the St. Petersburg Housing Authorit" in first.title


def test_titles_are_the_cms_own_truncated_summary_not_full_articles():
    """Confirms the real content-drift finding this module's docstring
    documents: the index's own title text ends in a literal '...'
    truncation marker, not full article content - the reason individual
    news-view pages are a blocker, not followed here."""
    html = load_fixture("news.html")
    items = make_crawler().parse_news_index(html, NEWS_URL)

    truncated = [i for i in items if i.title.endswith("...")]
    assert len(truncated) >= 1


def test_no_dollar_amounts_present_on_index_confirming_blocker():
    """DECISIONS #48's own recon quoted a $842K HUD Capital Fund award and
    a $104K FSS grant as reported "on its news pages" - this session's live
    recon found neither figure anywhere on this index. See module
    docstring / DECISIONS #51's blocker note."""
    html = load_fixture("news.html")
    assert "842" not in html
    assert "104,000" not in html and "$104" not in html


# --- Fail-loud: structure-parsing failures -----------------------------------


def test_parse_missing_title_raises():
    html = "<html><body><div id='cms-body-content'><div class='press-items'><a href='/news-view?id=1'><strong>01/01/2026</strong> - Title</a></div></div></body></html>"
    with pytest.raises(CrawlerStructureError, match="page title"):
        make_crawler().parse_news_index(html, NEWS_URL)


def test_parse_missing_items_container_raises():
    html = "<html><body><h2 class='ptitles'>Latest News</h2></body></html>"
    with pytest.raises(CrawlerStructureError, match="items container"):
        make_crawler().parse_news_index(html, NEWS_URL)


def test_parse_no_items_raises():
    html = "<html><body><h2 class='ptitles'>Latest News</h2><div id='cms-body-content'></div></body></html>"
    with pytest.raises(CrawlerStructureError, match="no .* news items"):
        make_crawler().parse_news_index(html, NEWS_URL)


def test_parse_item_missing_link_raises():
    html = """
    <html><body><h2 class="ptitles">Latest News</h2>
    <div id="cms-body-content">
        <div class="press-items">no link here</div>
    </div>
    </body></html>
    """
    with pytest.raises(CrawlerStructureError, match="no link to its article"):
        make_crawler().parse_news_index(html, NEWS_URL)


def test_parse_item_missing_date_raises():
    html = """
    <html><body><h2 class="ptitles">Latest News</h2>
    <div id="cms-body-content">
        <div class="press-items"><a href="/news-view?id=1">No date prefix here</a></div>
    </div>
    </body></html>
    """
    with pytest.raises(CrawlerStructureError, match="no <strong> date prefix"):
        make_crawler().parse_news_index(html, NEWS_URL)


def test_parse_item_bad_date_format_raises():
    html = """
    <html><body><h2 class="ptitles">Latest News</h2>
    <div id="cms-body-content">
        <div class="press-items"><a href="/news-view?id=1"><strong>2026-01-01</strong> - Title</a></div>
    </div>
    </body></html>
    """
    with pytest.raises(CrawlerStructureError, match="doesn't match expected"):
        make_crawler().parse_news_index(html, NEWS_URL)


def test_parse_item_empty_title_raises():
    html = """
    <html><body><h2 class="ptitles">Latest News</h2>
    <div id="cms-body-content">
        <div class="press-items"><a href="/news-view?id=1"><strong>01/01/2026</strong></a></div>
    </div>
    </body></html>
    """
    with pytest.raises(CrawlerStructureError, match="no title text"):
        make_crawler().parse_news_index(html, NEWS_URL)


# --- crawl() end-to-end against mocked HTTP ---------------------------------


@responses.activate
def test_crawl_fetches_and_parses():
    register_spha_robots(responses)
    responses.add(responses.GET, NEWS_URL, body=load_fixture("news.html"), status=200)

    crawler = make_crawler()
    items = crawler.crawl()

    assert len(items) == 20
    assert all(i.attribution.source_url == NEWS_URL for i in items)
