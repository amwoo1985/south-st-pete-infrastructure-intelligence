"""Tests for app/chunking/pinellas_hcd.py.

Uses real recorded fixtures run through PinellasHcdCrawler's own real
parse methods (parse_program_page(), parse_department_page()) - the same
real dataclass instances a live crawl would produce, never hand-built
stand-ins.
"""

from __future__ import annotations

from pathlib import Path

from app.chunking.pinellas_hcd import (
    DOC_TYPE_PINELLAS_DEPARTMENT_DESCRIPTION,
    DOC_TYPE_PINELLAS_PROGRAM_SECTION,
    DOC_TYPE_PINELLAS_STAT_CARD,
    chunk_pinellas_department_page,
    chunk_pinellas_department_pages,
    chunk_pinellas_program_page,
    chunk_pinellas_program_pages,
)
from app.crawlers.pinellas_hcd import (
    DEPARTMENT_OVERVIEW_URL,
    HOME_REPAIR_LOAN_PROGRAM_URL,
    HURRICANE_HOME_REPAIR_PROGRAM_URL,
    PinellasHcdCrawler,
)

FIXTURES_ROOT = Path(__file__).parent.parent / "fixtures"


def load(rel_path: str) -> str:
    return (FIXTURES_ROOT / rel_path).read_text(encoding="utf-8")


def load_real_program_page(name: str, url: str):
    crawler = PinellasHcdCrawler(min_request_interval_seconds=0)
    return crawler.parse_program_page(load(f"pinellas_hcd/{name}.html"), url)


def load_real_department_page():
    crawler = PinellasHcdCrawler(min_request_interval_seconds=0)
    return crawler.parse_department_page(load("pinellas_hcd/department.html"), DEPARTMENT_OVERVIEW_URL)


# --- 10 program pages: one chunk per flat section, regardless of level ------


def test_home_repair_page_produces_one_chunk_per_flat_section():
    # h3/h4/h5, no h2 at all (confirmed live) - flattened, not forced into
    # a hierarchy that isn't reliably there. 11 sections -> 10 chunks: the
    # contentless "Eligible Improvements" h3 (DECISIONS #67) forward-merges
    # into the next section instead of producing its own vacuous chunk.
    page = load_real_program_page("home-repair-loan-program", HOME_REPAIR_LOAN_PROGRAM_URL)
    assert len(page.sections) == 11
    assert page.sections[5].heading == "Eligible Improvements"
    assert not page.sections[5].text and not page.sections[5].links  # confirmed live: contentless

    chunks = chunk_pinellas_program_page(page)
    assert len(chunks) == 10
    assert all(c.doc_type == DOC_TYPE_PINELLAS_PROGRAM_SECTION for c in chunks)


def test_contentless_heading_forward_merges_into_next_section_only():
    # rag-review HIGH finding, DECISIONS #67: "Eligible Improvements" (h3,
    # no content of its own) must not produce its own chunk, and must not
    # be attached to every subsequent h4 - only the single next section
    # ("Home Repair Loan Program") gets the compound label; the section
    # after that ("Independent Living Program") stands alone.
    page = load_real_program_page("home-repair-loan-program", HOME_REPAIR_LOAN_PROGRAM_URL)
    chunks = chunk_pinellas_program_page(page)

    assert not any(c.section_label.endswith("— Eligible Improvements") for c in chunks)

    merged = [c for c in chunks if "Eligible Improvements › Home Repair Loan Program" in c.section_label]
    assert len(merged) == 1

    independent_living = next(c for c in chunks if c.section_label.endswith("— Independent Living Program"))
    assert "Eligible Improvements" not in independent_living.section_label


def test_hurricane_page_has_no_lead_section_but_chunks_every_heading():
    page = load_real_program_page(
        "pinellas-county-hurricane-home-repair-program", HURRICANE_HOME_REPAIR_PROGRAM_URL
    )
    assert page.sections[0].heading is not None  # confirmed live: no lead section on this page

    chunks = chunk_pinellas_program_page(page)
    assert len(chunks) == len(page.sections)


def test_section_chunk_carries_page_title_and_real_dollar_figures():
    page = load_real_program_page("home-repair-loan-program", HOME_REPAIR_LOAN_PROGRAM_URL)
    chunks = chunk_pinellas_program_page(page)
    assert all(page.page_title in c.text for c in chunks)
    assert any("$75,000" in c.text for c in chunks)  # real figure confirmed live


def test_program_section_chunk_ids_deterministic_and_unique():
    page = load_real_program_page("home-repair-loan-program", HOME_REPAIR_LOAN_PROGRAM_URL)
    ids_1 = [c.chunk_id for c in chunk_pinellas_program_page(page)]
    ids_2 = [c.chunk_id for c in chunk_pinellas_program_page(page)]
    assert ids_1 == ids_2
    assert len(ids_1) == len(set(ids_1))


def test_chunk_pinellas_program_pages_flattens_across_pages():
    # home-repair-loan-program: 11 sections -> 10 chunks (1 contentless
    # heading forward-merges, DECISIONS #67); hurricane page has no
    # contentless headings, so its count is unaffected (9 -> 9).
    page_a = load_real_program_page("home-repair-loan-program", HOME_REPAIR_LOAN_PROGRAM_URL)
    page_b = load_real_program_page(
        "pinellas-county-hurricane-home-repair-program", HURRICANE_HOME_REPAIR_PROGRAM_URL
    )
    chunks = chunk_pinellas_program_pages([page_a, page_b])
    assert len(chunks) == 19


# --- 1 department page: description + stat cards, hub sections excluded ----


def test_department_page_produces_description_and_stat_card_chunks():
    page = load_real_department_page()
    assert page.description  # real hero mission paragraph, confirmed live
    assert len(page.stat_cards) == 6  # confirmed live - not just the 2 the recon quote focused on
    assert len(page.hub_sections) == 5  # confirmed live

    chunks = chunk_pinellas_department_page(page)
    # 1 description chunk + 1 chunk per stat card, nothing for hub sections.
    assert len(chunks) == 1 + len(page.stat_cards)

    description_chunks = [c for c in chunks if c.doc_type == DOC_TYPE_PINELLAS_DEPARTMENT_DESCRIPTION]
    stat_card_chunks = [c for c in chunks if c.doc_type == DOC_TYPE_PINELLAS_STAT_CARD]
    assert len(description_chunks) == 1
    assert len(stat_card_chunks) == len(page.stat_cards)
    assert page.description in description_chunks[0].text


def test_hub_sections_produce_no_chunks_at_all():
    # The explicit exclusion this module's design calls for: hub_sections
    # carry no owned prose (PinellasHubSection has no `text` field at all)
    # - confirm chunking never invents a misleadingly "citable" chunk out
    # of bare link titles.
    page = load_real_department_page()
    chunks = chunk_pinellas_department_page(page)

    assert not any(c.doc_type not in (DOC_TYPE_PINELLAS_DEPARTMENT_DESCRIPTION, DOC_TYPE_PINELLAS_STAT_CARD) for c in chunks)

    hub_headings = {h.heading for h in page.hub_sections}
    hub_link_titles = {link.title for h in page.hub_sections for link in h.links}

    # No chunk's entire text is just a hub section's own heading or one of
    # its link titles - the only real content chunked is the description
    # and the stat cards.
    for chunk in chunks:
        assert chunk.text.strip() not in hub_headings
        assert chunk.text.strip() not in hub_link_titles


def test_stat_card_duplicate_heading_disambiguation():
    # rag-review MED finding, DECISIONS #67: defensive case not observed
    # live (all 6 real cards have distinct headings) - two stat cards
    # sharing heading text on one page must still get distinct, stable IDs
    # rather than one silently overwriting the other on upsert. Mirrors
    # test_stpete_pages.py's test_h2_section_duplicate_heading_disambiguation.
    from datetime import datetime, timezone

    from app.crawlers.base import Attribution
    from app.crawlers.pinellas_hcd import PinellasDepartmentPage, PinellasStatCard

    page = PinellasDepartmentPage(
        page_title="Test Department",
        page_url="https://pinellas.gov/department/test/",
        description=None,
        stat_cards=(
            PinellasStatCard(heading="Accomplishments", text="First accomplishment.", links=()),
            PinellasStatCard(heading="Accomplishments", text="Second accomplishment.", links=()),
        ),
        hub_sections=(),
        attribution=Attribution(
            source_url="https://pinellas.gov/department/test/",
            retrieval_timestamp=datetime.now(timezone.utc),
        ),
    )
    chunks = chunk_pinellas_department_page(page)
    assert len(chunks) == 2
    assert chunks[0].chunk_id != chunks[1].chunk_id
    assert "First accomplishment." in chunks[0].text
    assert "Second accomplishment." in chunks[1].text


def test_stat_card_chunk_ids_unique_and_deterministic():
    page = load_real_department_page()
    ids_1 = [c.chunk_id for c in chunk_pinellas_department_page(page)]
    ids_2 = [c.chunk_id for c in chunk_pinellas_department_page(page)]
    assert ids_1 == ids_2
    assert len(ids_1) == len(set(ids_1))


def test_chunk_pinellas_department_pages_wrapper():
    page = load_real_department_page()
    chunks = chunk_pinellas_department_pages([page])
    assert len(chunks) == 1 + len(page.stat_cards)
