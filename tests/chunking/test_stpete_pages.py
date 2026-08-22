"""Tests for app/chunking/stpete_pages.py.

Uses real recorded fixtures run through each crawler's own real parse
method (StpeteGrantsCrawler.parse_index()/parse_tiles_page(),
StpeteGrantCategoryPagesCrawler.parse_south_stpete_page()/
parse_sunrise_page(), StpeteProgramDetailsCrawler.parse_program_detail_page())
— the same real dataclasses a live crawl would produce — rather than
hand-built dataclass instances standing in for real site content.
"""

from __future__ import annotations

from pathlib import Path

from app.chunking.base import Chunk
from app.chunking.stpete_pages import (
    DOC_TYPE_GRANT_CATEGORY,
    DOC_TYPE_PROGRAM_DETAIL,
    DOC_TYPE_PROGRAM_SUMMARY,
    DOC_TYPE_SUNRISE_PROGRAM,
    chunk_stpete_grant_categories,
    chunk_stpete_grant_category,
    chunk_stpete_program_detail_page,
    chunk_stpete_program_detail_pages,
    chunk_stpete_program_summaries,
    chunk_stpete_sunrise_programs,
)
from app.crawlers.stpete_grant_categories import (
    SOUTH_STPETE_URL,
    SUNRISE_URL,
    StpeteGrantCategoryPagesCrawler,
)
from app.crawlers.stpete_grants import GRANTS_URL, StpeteGrantsCrawler
from app.crawlers.stpete_program_details import (
    FOR_PROPERTY_OWNERS_URL,
    INDIVIDUAL_ARTIST_GRANT_URL,
    StpeteProgramDetailsCrawler,
)

FIXTURES_ROOT = Path(__file__).parent.parent / "fixtures"


def load(rel_path: str) -> str:
    return (FIXTURES_ROOT / rel_path).read_text(encoding="utf-8")


# --- Index-page tiles (StpeteGrantCategory) ---------------------------------


def test_index_tiles_produce_one_chunk_each():
    crawler = StpeteGrantsCrawler(min_request_interval_seconds=0)
    categories = crawler.parse_index(load("stpete_grants/grants_index.html"))
    assert len(categories) == 6  # DECISIONS #27 - 6 categories confirmed live

    chunks = chunk_stpete_grant_categories(categories)
    assert len(chunks) == 6
    assert all(c.doc_type == DOC_TYPE_GRANT_CATEGORY for c in chunks)
    assert all(c.attribution.source_url == GRANTS_URL for c in chunks)


def test_index_tile_chunk_text_includes_category_name():
    crawler = StpeteGrantsCrawler(min_request_interval_seconds=0)
    categories = crawler.parse_index(load("stpete_grants/grants_index.html"))
    chunk = chunk_stpete_grant_category(categories[0])
    assert categories[0].category_name in chunk.text


def test_index_tile_chunk_ids_are_deterministic_and_unique():
    crawler = StpeteGrantsCrawler(min_request_interval_seconds=0)
    categories = crawler.parse_index(load("stpete_grants/grants_index.html"))
    ids_1 = [chunk_stpete_grant_category(c).chunk_id for c in categories]
    ids_2 = [chunk_stpete_grant_category(c).chunk_id for c in categories]
    assert ids_1 == ids_2
    assert len(ids_1) == len(set(ids_1))


# --- Hub-page tiles (business.php etc — same StpeteGrantCategory shape) ----


def test_hub_page_tiles_reuse_the_same_chunker():
    crawler = StpeteGrantsCrawler(min_request_interval_seconds=0)
    business_url = "https://www.stpete.org/residents/grants___loans/business.php"
    categories = crawler.parse_tiles_page(load("stpete_grant_categories/business.html"), business_url)
    assert len(categories) > 0

    chunks = chunk_stpete_grant_categories(categories)
    assert len(chunks) == len(categories)
    assert all(c.attribution.source_url == business_url for c in chunks)


# --- for_south_stpete.php freeform program blocks ---------------------------


def test_south_stpete_programs_produce_one_chunk_each_with_description():
    crawler = StpeteGrantCategoryPagesCrawler(min_request_interval_seconds=0)
    programs = crawler.parse_south_stpete_page(load("stpete_grant_categories/for_south_stpete.html"))
    assert len(programs) > 0

    chunks = chunk_stpete_program_summaries(programs)
    assert len(chunks) == len(programs)
    assert all(c.doc_type == DOC_TYPE_PROGRAM_SUMMARY for c in chunks)
    assert all(c.attribution.source_url == SOUTH_STPETE_URL for c in chunks)
    # Every real program on this page has a description (confirmed live,
    # DECISIONS #33) - the chunk text must carry it, not just the name.
    for program, chunk in zip(programs, chunks):
        if program.description:
            assert program.description in chunk.text


# --- Sunrise St. Pete tiles ---------------------------------------------------


def test_sunrise_programs_include_eligibility_and_description():
    crawler = StpeteGrantCategoryPagesCrawler(min_request_interval_seconds=0)
    programs = crawler.parse_sunrise_page(load("stpete_grant_categories/sunrise_index.html"))
    assert len(programs) > 0

    chunks = chunk_stpete_sunrise_programs(programs)
    assert len(chunks) == len(programs)
    assert all(c.doc_type == DOC_TYPE_SUNRISE_PROGRAM for c in chunks)
    assert all(c.attribution.source_url == SUNRISE_URL for c in chunks)

    for program, chunk in zip(programs, chunks):
        if program.eligibility:
            assert program.eligibility in chunk.text
        if program.description:
            assert program.description in chunk.text


# --- H2-sectioned detail pages: the ambiguous-h2 case (DECISIONS #60) ------


def test_multi_program_page_produces_one_chunk_per_h2_regardless_of_ambiguity():
    # for_property_owners.php: 13 <h2>s, each a distinct program (DECISIONS
    # #37) - the ambiguous case this design deliberately does not try to
    # disambiguate.
    crawler = StpeteProgramDetailsCrawler(min_request_interval_seconds=0)
    page = crawler.parse_program_detail_page(
        load("stpete_program_details/for_property_owners.html"), FOR_PROPERTY_OWNERS_URL
    )
    assert len(page.sections) >= 10  # DECISIONS #37 names 13

    chunks = chunk_stpete_program_detail_page(page)
    assert len(chunks) == len(page.sections)
    assert all(c.doc_type == DOC_TYPE_PROGRAM_DETAIL for c in chunks)


def test_single_program_page_produces_one_chunk_per_generic_section():
    # individual_artist_grant.php: <h2>s are generic section labels
    # (Overview/Eligibility/...) for one program (DECISIONS #37) - still
    # one chunk per <h2>, same rule applied uniformly.
    crawler = StpeteProgramDetailsCrawler(min_request_interval_seconds=0)
    page = crawler.parse_program_detail_page(
        load("stpete_program_details/individual_artist_grant.html"), INDIVIDUAL_ARTIST_GRANT_URL
    )
    chunks = chunk_stpete_program_detail_page(page)
    assert len(chunks) == len(page.sections)


def test_h2_section_chunk_carries_page_title_as_context():
    # A chunk pulled out of a 13-program page must still self-identify
    # which page it came from - it's not just the raw <h2> text.
    crawler = StpeteProgramDetailsCrawler(min_request_interval_seconds=0)
    page = crawler.parse_program_detail_page(
        load("stpete_program_details/for_property_owners.html"), FOR_PROPERTY_OWNERS_URL
    )
    chunks = chunk_stpete_program_detail_page(page)
    for chunk in chunks:
        assert page.page_title in chunk.text
        assert page.page_title in chunk.section_label


def test_h2_section_chunk_folds_in_subsection_text():
    crawler = StpeteProgramDetailsCrawler(min_request_interval_seconds=0)
    page = crawler.parse_program_detail_page(
        load("stpete_program_details/individual_artist_grant.html"), INDIVIDUAL_ARTIST_GRANT_URL
    )
    chunks = chunk_stpete_program_detail_page(page)
    section_by_heading = {s.heading: s for s in page.sections}

    for chunk in chunks:
        heading = chunk.section_label.split(" — ", 1)[1]
        section = section_by_heading[heading]
        for subsection in section.subsections:
            assert subsection.heading in chunk.text
            if subsection.text:
                assert subsection.text in chunk.text


def test_h2_section_chunk_ids_are_deterministic_and_unique_within_a_page():
    crawler = StpeteProgramDetailsCrawler(min_request_interval_seconds=0)
    page = crawler.parse_program_detail_page(
        load("stpete_program_details/for_property_owners.html"), FOR_PROPERTY_OWNERS_URL
    )
    ids_1 = [c.chunk_id for c in chunk_stpete_program_detail_page(page)]
    ids_2 = [c.chunk_id for c in chunk_stpete_program_detail_page(page)]
    assert ids_1 == ids_2
    assert len(ids_1) == len(set(ids_1))


def test_h2_section_duplicate_heading_disambiguation():
    # Defensive case not observed live (DECISIONS #60) - two sections with
    # the same heading text on one page must still get distinct, stable
    # IDs rather than colliding or crashing.
    from datetime import datetime, timezone

    from app.crawlers.base import Attribution
    from app.crawlers.stpete_program_details import (
        StpeteProgramDetailPage,
        StpeteProgramSection,
    )

    page = StpeteProgramDetailPage(
        page_title="Test Page",
        page_url="https://www.stpete.org/test.php",
        intro=None,
        sections=(
            StpeteProgramSection(heading="Overview", text="First overview.", subsections=(), links=()),
            StpeteProgramSection(heading="Overview", text="Second overview.", subsections=(), links=()),
        ),
        attribution=Attribution(
            source_url="https://www.stpete.org/test.php",
            retrieval_timestamp=datetime.now(timezone.utc),
        ),
    )
    chunks = chunk_stpete_program_detail_page(page)
    assert len(chunks) == 2
    assert chunks[0].chunk_id != chunks[1].chunk_id
    assert "First overview." in chunks[0].text
    assert "Second overview." in chunks[1].text


def test_chunk_stpete_program_detail_pages_flattens_across_pages():
    crawler = StpeteProgramDetailsCrawler(min_request_interval_seconds=0)
    page_a = crawler.parse_program_detail_page(
        load("stpete_program_details/individual_artist_grant.html"), INDIVIDUAL_ARTIST_GRANT_URL
    )
    page_b = crawler.parse_program_detail_page(
        load("stpete_program_details/for_property_owners.html"), FOR_PROPERTY_OWNERS_URL
    )
    chunks = chunk_stpete_program_detail_pages([page_a, page_b])
    assert len(chunks) == len(page_a.sections) + len(page_b.sections)
