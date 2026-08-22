"""Tests for app/chunking/row_based.py.

Uses real recorded fixtures run through each crawler's own real parse
method (PinellasCFCrawler.parse_grants_table(),
StpeteArpaCrawler.parse_arpa_page()) - the same real dataclasses a live
crawl would produce.
"""

from __future__ import annotations

from pathlib import Path

from app.chunking.row_based import (
    DOC_TYPE_ARPA_ALLOCATION,
    DOC_TYPE_PINELLAS_CF_GRANT,
    chunk_arpa_allocation,
    chunk_arpa_allocations,
    chunk_pinellas_cf_program,
    chunk_pinellas_cf_programs,
)
from app.crawlers.pinellas_cf import GRANTS_URL as PINELLAS_CF_URL
from app.crawlers.pinellas_cf import PinellasCFCrawler
from app.crawlers.stpete_arpa import ARPA_URL, StpeteArpaCrawler

FIXTURES_ROOT = Path(__file__).parent.parent / "fixtures"


def load(rel_path: str) -> str:
    return (FIXTURES_ROOT / rel_path).read_text(encoding="utf-8")


def load_real_pinellas_cf_programs():
    crawler = PinellasCFCrawler(min_request_interval_seconds=0)
    return crawler.parse_grants_table(load("pinellas_cf/grants_index.html"))


def load_real_arpa_allocations():
    crawler = StpeteArpaCrawler(min_request_interval_seconds=0)
    return crawler.parse_arpa_page(load("stpete_arpa/american_rescue_plan_act.html"), ARPA_URL)


# --- Pinellas CF: one row = one chunk ---------------------------------------


def test_pinellas_cf_chunks_one_per_row():
    programs = load_real_pinellas_cf_programs()
    chunks = chunk_pinellas_cf_programs(programs)
    assert len(chunks) == len(programs)
    assert all(c.doc_type == DOC_TYPE_PINELLAS_CF_GRANT for c in chunks)


def test_pinellas_cf_same_underlying_program_different_cycles_get_distinct_chunks():
    # "Senior Citizens Services Grants: Housing"/"...: Wellness"/"...:
    # Support" share one detail_url but are 3 distinct funding-cycle rows
    # (DECISIONS #63) - each must get its own chunk_id.
    programs = load_real_pinellas_cf_programs()
    senior_programs = [p for p in programs if p.program_name.startswith("Senior Citizens Services Grants")]
    assert len(senior_programs) == 3

    chunks = [chunk_pinellas_cf_program(p) for p in senior_programs]
    ids = {c.chunk_id for c in chunks}
    assert len(ids) == 3
    # Each cycle's own timeline/distribution text must be distinguishable.
    for program, chunk in zip(senior_programs, chunks):
        assert program.application_timeline in chunk.text
        assert program.award_distribution in chunk.text


def test_pinellas_cf_chunk_attribution_passthrough():
    programs = load_real_pinellas_cf_programs()
    chunks = chunk_pinellas_cf_programs(programs)
    for program, chunk in zip(programs, chunks):
        assert chunk.attribution is program.attribution
        assert chunk.attribution.source_url == PINELLAS_CF_URL


def test_pinellas_cf_chunking_twice_is_idempotent():
    programs = load_real_pinellas_cf_programs()
    ids_1 = [c.chunk_id for c in chunk_pinellas_cf_programs(programs)]
    ids_2 = [c.chunk_id for c in chunk_pinellas_cf_programs(programs)]
    assert ids_1 == ids_2
    assert len(ids_1) == len(set(ids_1))


# --- ARPA: one allocation = one chunk ---------------------------------------


def test_arpa_chunks_one_per_allocation():
    allocations = load_real_arpa_allocations()
    assert len(allocations) == 10  # DECISIONS #45 - 10 confirmed live

    chunks = chunk_arpa_allocations(allocations)
    assert len(chunks) == 10
    assert all(c.doc_type == DOC_TYPE_ARPA_ALLOCATION for c in chunks)


def test_arpa_chunk_text_includes_amount_and_description():
    allocations = load_real_arpa_allocations()
    chunk = chunk_arpa_allocation(allocations[0])
    assert allocations[0].amount_text in chunk.text
    assert allocations[0].description in chunk.text
    assert allocations[0].category in chunk.text


def test_arpa_chunk_ids_are_unique_across_all_10_allocations():
    allocations = load_real_arpa_allocations()
    chunks = chunk_arpa_allocations(allocations)
    ids = {c.chunk_id for c in chunks}
    assert len(ids) == 10


def test_arpa_chunk_attribution_has_no_published_date():
    # DECISIONS #45: no clean per-allocation date exists - nullable, not a
    # sentinel.
    allocations = load_real_arpa_allocations()
    chunks = chunk_arpa_allocations(allocations)
    assert all(c.attribution.published_date is None for c in chunks)


def test_arpa_chunking_twice_is_idempotent():
    allocations = load_real_arpa_allocations()
    ids_1 = [c.chunk_id for c in chunk_arpa_allocations(allocations)]
    ids_2 = [c.chunk_id for c in chunk_arpa_allocations(allocations)]
    assert ids_1 == ids_2
