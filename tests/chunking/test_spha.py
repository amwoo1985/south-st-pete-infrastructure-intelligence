"""Tests for app/chunking/spha.py.

Uses real recorded fixtures run through each crawler's own real parse
method (SphaProgramPagesCrawler.parse_program_page(),
SphaNewsCrawler.parse_news_index()) - the same real dataclass instances a
live crawl would produce, never hand-built stand-ins.
"""

from __future__ import annotations

from pathlib import Path

from app.chunking.spha import (
    DOC_TYPE_SPHA_NEWS_ITEM,
    DOC_TYPE_SPHA_PROGRAM_SECTION,
    chunk_spha_news_items,
    chunk_spha_program_page,
    chunk_spha_program_pages,
)
from app.crawlers.spha_news import NEWS_URL, SphaNewsCrawler
from app.crawlers.spha_program_pages import (
    HOUSING_URL,
    PERFORMANCE_REPORT_URL,
    PUBLIC_HOUSING_CLIENTS_URL,
    SphaProgramPagesCrawler,
)

FIXTURES_ROOT = Path(__file__).parent.parent / "fixtures"


def load(rel_path: str) -> str:
    return (FIXTURES_ROOT / rel_path).read_text(encoding="utf-8")


def load_real_page(name: str, url: str):
    crawler = SphaProgramPagesCrawler(min_request_interval_seconds=0)
    return crawler.parse_program_page(load(f"spha_program_pages/{name}.html"), url)


def load_real_news_items():
    crawler = SphaNewsCrawler(min_request_interval_seconds=0)
    return crawler.parse_news_index(load("spha_news/news.html"), NEWS_URL)


# --- Program pages: one chunk per section ------------------------------------


def test_public_housing_clients_produces_one_chunk_per_section():
    page = load_real_page("public-housing-clients", PUBLIC_HOUSING_CLIENTS_URL)
    assert len(page.sections) == 7  # confirmed live: lead + FAQ + 5 topic sections

    chunks = chunk_spha_program_page(page)
    assert len(chunks) == 7
    assert all(c.doc_type == DOC_TYPE_SPHA_PROGRAM_SECTION for c in chunks)


def test_lead_section_gets_its_own_chunk_with_introduction_label():
    page = load_real_page("housing", HOUSING_URL)
    assert page.sections[0].heading is None  # confirmed live: real lead content

    chunks = chunk_spha_program_page(page)
    lead_chunks = [c for c in chunks if "Introduction" in c.section_label]
    assert len(lead_chunks) == 1
    assert page.sections[0].text in lead_chunks[0].text


def test_performance_report_has_no_headings_and_produces_one_chunk():
    # Degenerate case: zero 36px headings anywhere on the page (confirmed
    # live) - one heading=None section covering the whole page, same
    # "one section, one chunk" rule applied uniformly.
    page = load_real_page("performance-report", PERFORMANCE_REPORT_URL)
    assert len(page.sections) == 1
    assert page.sections[0].heading is None

    chunks = chunk_spha_program_page(page)
    assert len(chunks) == 1


def test_section_chunk_carries_page_title_as_context():
    page = load_real_page("public-housing-clients", PUBLIC_HOUSING_CLIENTS_URL)
    chunks = chunk_spha_program_page(page)
    for chunk in chunks:
        assert page.page_title in chunk.text
        assert page.page_title in chunk.section_label


def test_section_chunk_ids_are_deterministic_and_unique():
    page = load_real_page("public-housing-clients", PUBLIC_HOUSING_CLIENTS_URL)
    ids_1 = [c.chunk_id for c in chunk_spha_program_page(page)]
    ids_2 = [c.chunk_id for c in chunk_spha_program_page(page)]
    assert ids_1 == ids_2
    assert len(ids_1) == len(set(ids_1))


def test_chunk_spha_program_pages_flattens_across_pages():
    page_a = load_real_page("housing", HOUSING_URL)
    page_b = load_real_page("public-housing-clients", PUBLIC_HOUSING_CLIENTS_URL)
    chunks = chunk_spha_program_pages([page_a, page_b])
    assert len(chunks) == len(page_a.sections) + len(page_b.sections)


# --- News index: one chunk per item, item_url as identity --------------------


def test_news_items_produce_one_chunk_each():
    items = load_real_news_items()
    assert len(items) == 20  # this CMS's default page size, confirmed live

    chunks = chunk_spha_news_items(items)
    assert len(chunks) == 20
    assert all(c.doc_type == DOC_TYPE_SPHA_NEWS_ITEM for c in chunks)


def test_news_chunk_text_notes_truncation_and_carries_item_url():
    items = load_real_news_items()
    chunks = chunk_spha_news_items(items)
    for item, chunk in zip(items, chunks):
        assert item.title in chunk.text
        assert item.item_url in chunk.text
        assert "truncated" in chunk.text  # honest about the CMS's own "..." truncation


def test_news_chunk_ids_unique_across_all_20_items():
    items = load_real_news_items()
    chunks = chunk_spha_news_items(items)
    ids = {c.chunk_id for c in chunks}
    assert len(ids) == 20


def test_news_chunk_ids_deterministic_across_reruns():
    items = load_real_news_items()
    ids_1 = [c.chunk_id for c in chunk_spha_news_items(items)]
    ids_2 = [c.chunk_id for c in chunk_spha_news_items(items)]
    assert ids_1 == ids_2


def test_news_chunk_attribution_has_real_published_date():
    # Unlike every other DECISIONS #48 page, this CMS's own date-prefix
    # convention is a real structural signal - not nullable here.
    items = load_real_news_items()
    chunks = chunk_spha_news_items(items)
    assert all(c.attribution.published_date is not None for c in chunks)
