"""Tests for app/chunking/ami_table.py.

Uses the real recorded income_limits.php fixture
(tests/fixtures/stpete_income_limits/income_limits.html) run through the
real crawler's own parse_income_limits_page() — the same 120 AMIThreshold
rows a live crawl would produce. See DECISIONS #61 for why this groups by
program instead of chunking 1:1 per row.
"""

from __future__ import annotations

from pathlib import Path

from app.chunking.ami_table import DOC_TYPE_AMI_THRESHOLD, chunk_ami_thresholds
from app.crawlers.stpete_income_limits import INCOME_LIMITS_URL, StpeteIncomeLimitsCrawler

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "stpete_income_limits"


def load_real_thresholds():
    html = (FIXTURES_DIR / "income_limits.html").read_text(encoding="utf-8")
    crawler = StpeteIncomeLimitsCrawler(min_request_interval_seconds=0)
    return crawler.parse_income_limits_page(html, INCOME_LIMITS_URL)


# --- Not 1:1 per row ---------------------------------------------------------


def test_chunks_are_far_fewer_than_rows():
    thresholds = load_real_thresholds()
    assert len(thresholds) == 120  # 8 household sizes x 15 program-column pairs

    chunks = chunk_ami_thresholds(thresholds)
    # 6 named programs (SHIP/HOME/NSP/CDBG-DR/TIF/WFH) + 1 no-program
    # (bare 100% AMI) group, per DECISIONS #44's live-confirmed program set.
    assert len(chunks) == 7
    assert all(c.doc_type == DOC_TYPE_AMI_THRESHOLD for c in chunks)


def test_every_named_program_gets_its_own_chunk():
    thresholds = load_real_thresholds()
    chunks = chunk_ami_thresholds(thresholds)
    labels = {c.section_label for c in chunks}
    for program in ("SHIP", "HOME", "NSP", "CDBG-DR", "TIF", "WFH"):
        assert program in labels


# --- Serves the real query shape: household size + program -----------------


def test_ship_chunk_contains_every_household_size_and_percent_tier_ship_appears_at():
    thresholds = load_real_thresholds()
    ship_rows = [t for t in thresholds if t.program == "SHIP"]
    assert ship_rows  # sanity: SHIP rows exist in this fixture

    chunks = chunk_ami_thresholds(thresholds)
    ship_chunk = next(c for c in chunks if c.section_label == "SHIP")

    for row in ship_rows:
        expected_line = f"Household size {row.household_size}, {row.ami_percent}% AMI: ${row.dollar_amount:,}"
        assert expected_line in ship_chunk.text

    # SHIP appears at more than one AMI percent tier on the real page
    # (DECISIONS #44: 30%/120%/140%) - confirms the group-by-program
    # design actually gathers across tiers, not just within one.
    percents_present = {row.ami_percent for row in ship_rows}
    assert len(percents_present) > 1


def test_household_size_4_ship_query_is_answerable_from_one_chunk():
    # The exact query shape named in the task: "what's the income limit
    # for a household of 4 under SHIP".
    thresholds = load_real_thresholds()
    ship_size_4 = next(t for t in thresholds if t.program == "SHIP" and t.household_size == 4)

    chunks = chunk_ami_thresholds(thresholds)
    ship_chunk = next(c for c in chunks if c.section_label == "SHIP")
    assert f"Household size 4" in ship_chunk.text
    assert f"${ship_size_4.dollar_amount:,}" in ship_chunk.text


# --- Attribution / effective-date passthrough -------------------------------


def test_named_program_chunk_carries_that_programs_effective_date():
    thresholds = load_real_thresholds()
    ship_rows = [t for t in thresholds if t.program == "SHIP"]
    expected_date = ship_rows[0].attribution.published_date
    assert expected_date is not None  # SHIP has a stated effective date (DECISIONS #44)

    chunks = chunk_ami_thresholds(thresholds)
    ship_chunk = next(c for c in chunks if c.section_label == "SHIP")
    assert ship_chunk.attribution.published_date == expected_date


def test_no_program_group_has_no_effective_date():
    # The bare 100% AMI column has no program name and no stated effective
    # date anywhere on the page (DECISIONS #44) - nullable, not a sentinel.
    thresholds = load_real_thresholds()
    chunks = chunk_ami_thresholds(thresholds)
    no_program_chunk = next(c for c in chunks if c.section_label.startswith("100% AMI"))
    assert no_program_chunk.attribution.published_date is None


def test_tif_and_wfh_groups_have_no_stated_effective_date():
    # DECISIONS #44: the page's <h2> only states dates for
    # SHIP/HOME/NSP/CDBG-DR - TIF and WFH have none anywhere on the page.
    thresholds = load_real_thresholds()
    chunks = chunk_ami_thresholds(thresholds)
    for label in ("TIF", "WFH"):
        chunk = next(c for c in chunks if c.section_label == label)
        assert chunk.attribution.published_date is None


# --- Determinism / idempotency ----------------------------------------------


def test_chunking_the_same_thresholds_twice_produces_identical_ids():
    thresholds = load_real_thresholds()
    ids_1 = [c.chunk_id for c in chunk_ami_thresholds(thresholds)]
    ids_2 = [c.chunk_id for c in chunk_ami_thresholds(thresholds)]
    assert ids_1 == ids_2
    assert len(ids_1) == len(set(ids_1))


# --- Empty input --------------------------------------------------------------


def test_empty_thresholds_produces_no_chunks():
    assert chunk_ami_thresholds([]) == []
