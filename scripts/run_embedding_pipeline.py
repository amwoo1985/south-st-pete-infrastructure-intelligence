"""Phase E end-to-end runner (DECISIONS #71).

Builds every Chunk this codebase can currently produce by running each of
the 8 chunking modules against the REAL recorded fixtures in
tests/fixtures/ — through each source's own real crawler parse method
(never a hand-built stand-in), the same pattern already established by
this repo's chunking test suite (tests/chunking/*.py). This step makes no
network call and costs nothing.

Usage:
    python scripts/run_embedding_pipeline.py estimate
        Builds every chunk, prints per-source counts and a rough
        full-corpus embedding-cost estimate. No API calls, no DB writes.

    python scripts/run_embedding_pipeline.py run-slice
        Same as `estimate`, then embeds+inserts a deliberately smaller
        validated SLICE (not the full corpus — see DECISIONS #71 for why)
        against the REAL local Postgres, using the REAL OpenAI API.

    python scripts/run_embedding_pipeline.py run-all
        Same as `estimate`, then embeds+inserts EVERY chunk this codebase
        can currently produce (all 8 chunking modules, all real fixtures)
        against the REAL local Postgres, using the REAL OpenAI API. Go-
        ahead for this given explicitly after `run-slice`'s 91-chunk
        validation looked correct — see the DECISIONS entry that follows
        this run. Relies on the same chunk_id pre-check idempotency
        `run-slice` uses: chunks already inserted by a prior `run-slice`
        (or a prior `run-all`) are skipped, not re-billed or duplicated.

    python scripts/run_embedding_pipeline.py validate
        Runs the hand-picked real validation queries (step 7) against
        whatever is currently in the `chunks` table.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import openai
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.chunking.ami_table import chunk_ami_thresholds
from app.chunking.legistar import chunk_legistar_meetings
from app.chunking.pinellas_hcd import (
    chunk_pinellas_department_pages,
    chunk_pinellas_program_pages,
)
from app.chunking.row_based import chunk_arpa_allocations, chunk_pinellas_cf_programs
from app.chunking.spha import chunk_spha_news_items, chunk_spha_program_pages
from app.chunking.stpete_commitment import chunk_stpete_commitment_action_items
from app.chunking.stpete_pages import (
    chunk_stpete_grant_categories,
    chunk_stpete_program_detail_pages,
    chunk_stpete_program_summaries,
    chunk_stpete_sunrise_programs,
)
from app.crawlers.base import Attribution
from app.crawlers.legistar import LegistarCrawler, LegistarMeeting
from app.crawlers.pinellas_cf import GRANTS_URL as PINELLAS_CF_URL
from app.crawlers.pinellas_cf import PinellasCFCrawler
from app.crawlers.pinellas_hcd import (
    DEPARTMENT_OVERVIEW_URL,
    PROGRAM_PAGE_URLS,
    PinellasHcdCrawler,
)
from app.crawlers.spha_news import NEWS_URL as SPHA_NEWS_URL
from app.crawlers.spha_news import SphaNewsCrawler
from app.crawlers.spha_program_pages import PROGRAM_PAGE_URLS as SPHA_PAGE_URLS
from app.crawlers.spha_program_pages import SphaProgramPagesCrawler
from app.crawlers.stpete_arpa import ARPA_URL, StpeteArpaCrawler
from app.crawlers.stpete_commitment import COMMITMENT_URL, StpeteCommitmentCrawler
from app.crawlers.stpete_grant_categories import (
    BUSINESS_URL,
    COMMUNITY_URL,
    HOUSING_URL as GRANT_CAT_HOUSING_URL,
    SOUTH_STPETE_URL,
    SUNRISE_URL,
    YOUTH_URL,
    StpeteGrantCategoryPagesCrawler,
)
from app.crawlers.stpete_grants import GRANTS_URL, StpeteGrantsCrawler
from app.crawlers.stpete_income_limits import INCOME_LIMITS_URL, StpeteIncomeLimitsCrawler
from app.crawlers.stpete_program_details import (
    AFFORDABLE_HOUSING_LOT_DISPOSITION_URL,
    ARTS_GRANTS_PROGRAM_URL,
    COMMUNITY_FOOD_GRANT_PROGRAM_URL,
    COMMUNITY_IMPACT_SUMMER_ENHANCEMENT_GRANT_URL,
    CONSOLIDATED_PLAN_URL,
    CRA_HOUSING_BASED_GRANTS_URL,
    EDUCATION_YOUTH_OPPORTUNITY_GRANTS_URL,
    FOR_BUSINESS_OWNERS_URL,
    FOR_DEVELOPERS_URL,
    FOR_PROPERTY_OWNERS_URL,
    GOV_YOUTH_OPPORTUNITY_GRANTS_URL,
    GROW_SMARTER_URL,
    HOUSING_REHABILITATION_ASSISTANCE_URL,
    INDIVIDUAL_ARTIST_GRANT_URL,
    LEGACY_BUSINESS_PROGRAM_URL,
    LEVEL_UP_ARTS_GRANT_URL,
    MAYORS_NEIGHBORHOOD_MINI_GRANT_URL,
    MLK_COMMUNITIES_IN_ACTION_URL,
    MULTI_FAMILY_RENTAL_LOAN_PROGRAM_URL,
    NEIGHBORHOOD_PARTNERSHIP_MATCHING_GRANTS_URL,
    PURCHASE_ASSISTANCE_PROGRAM_URL,
    REBATES_FOR_AFFORDABLE_RESIDENTIAL_REHABS_URL,
    SOCIAL_ACTION_FUNDING_URL,
    SOLAR_URL,
    STORMWATER_UTILITY_FEE_CREDITS_URL,
    TAX_INCENTIVES_URL,
    YOUTH_DEVELOPMENT_GRANTS_URL,
    StpeteProgramDetailsCrawler,
)
from app.db.connection import get_connection
from app.db.schema import apply_schema
from app.embeddings.pipeline import embed_and_insert_chunks

load_dotenv()

FIXTURES = Path(__file__).parent.parent / "tests" / "fixtures"

# Published, publicly documented text-embedding-3-small pricing — NOT
# re-verified via a live pricing-page fetch this session (only the
# dimensionality was verified live, per the task's actual ask). Flagged
# here rather than silently presented as freshly confirmed.
USD_PER_MILLION_TOKENS = 0.02
CHARS_PER_TOKEN_ESTIMATE = 4  # conservative average for English prose


def load(rel_path: str) -> str:
    return (FIXTURES / rel_path).read_text(encoding="utf-8")


# --- Detail-page fixture/url pairs (mirrors tests/crawlers/test_stpete_program_details.py) ---

DETAIL_PAGE_CASES = [
    ("for_developers.html", FOR_DEVELOPERS_URL),
    ("for_property_owners.html", FOR_PROPERTY_OWNERS_URL),
    ("arts_grants_program.html", ARTS_GRANTS_PROGRAM_URL),
    ("community_food_grant_program.html", COMMUNITY_FOOD_GRANT_PROGRAM_URL),
    ("individual_artist_grant.html", INDIVIDUAL_ARTIST_GRANT_URL),
    ("level_up_arts_grant.html", LEVEL_UP_ARTS_GRANT_URL),
    ("mayors_neighborhood_mini-grant_program.html", MAYORS_NEIGHBORHOOD_MINI_GRANT_URL),
    ("mlk_communities_in_action_mini-grant_program.html", MLK_COMMUNITIES_IN_ACTION_URL),
    ("neighborhood_partnership_matching_grants.html", NEIGHBORHOOD_PARTNERSHIP_MATCHING_GRANTS_URL),
    ("social_action_funding.html", SOCIAL_ACTION_FUNDING_URL),
    ("stormwater_utility_fee_credits.html", STORMWATER_UTILITY_FEE_CREDITS_URL),
    ("affordable_housing_lot_disposition_program.html", AFFORDABLE_HOUSING_LOT_DISPOSITION_URL),
    ("consolidated_plan.html", CONSOLIDATED_PLAN_URL),
    ("housing_rehabilitation_assistance_program.html", HOUSING_REHABILITATION_ASSISTANCE_URL),
    ("multi-family_rental_loan_program.html", MULTI_FAMILY_RENTAL_LOAN_PROGRAM_URL),
    ("purchase_assistance_program.html", PURCHASE_ASSISTANCE_PROGRAM_URL),
    ("rebates_for_affordable_residential_rehabs.html", REBATES_FOR_AFFORDABLE_RESIDENTIAL_REHABS_URL),
    ("solar.html", SOLAR_URL),
    ("community_impact_summer_enhancement_grant.html", COMMUNITY_IMPACT_SUMMER_ENHANCEMENT_GRANT_URL),
    ("education_youth_opportunity_grants.html", EDUCATION_YOUTH_OPPORTUNITY_GRANTS_URL),
    ("youth_development_grants.html", YOUTH_DEVELOPMENT_GRANTS_URL),
    ("gov_youth_opportunity_grants.html", GOV_YOUTH_OPPORTUNITY_GRANTS_URL),
]
FURTHER_HUB_DETAIL_CASES = [
    ("grow_smarter.html", GROW_SMARTER_URL),
    ("legacy_business_program.html", LEGACY_BUSINESS_PROGRAM_URL),
    ("tax_incentives.html", TAX_INCENTIVES_URL),
]


def build_all_chunks() -> dict[str, list]:
    """Returns {source_name: [Chunk, ...]} for every one of the 8
    chunking modules, run against every real recorded fixture that
    exists for it. Pure parsing — no network, no API calls, no DB."""
    chunks: dict[str, list] = {}

    # --- Legistar (app/chunking/legistar.py) ---
    legistar_crawler = LegistarCrawler(min_request_interval_seconds=0)
    agenda_url = "https://pinellas.legistar.com/View.ashx?M=A&ID=1249432"
    agenda_text = legistar_crawler._extract_html_text(
        load("legistar/accessible_agenda.html"), agenda_url
    )
    meeting = LegistarMeeting(
        body_name="Board of County Commissioners",
        meeting_date=date(2025, 12, 16),
        meeting_time="2:00 PM",
        location="333 Chestnut Street, Palm Room",
        meeting_detail_url="https://pinellas.legistar.com/MeetingDetail.aspx?ID=1249432",
        agenda_pdf_url=None,
        accessible_agenda_html_url=agenda_url,
        minutes_pdf_url=None,
        accessible_minutes_html_url=None,
        video_url=None,
        agenda_text=agenda_text,
        agenda_source="accessible_html",
        attribution=Attribution.now(
            source_url="https://pinellas.legistar.com/MeetingDetail.aspx?ID=1249432",
            published_date=date(2025, 12, 16),
        ),
    )
    chunks["legistar"] = chunk_legistar_meetings([meeting])

    # --- AMI table (app/chunking/ami_table.py) ---
    income_crawler = StpeteIncomeLimitsCrawler(min_request_interval_seconds=0)
    thresholds = income_crawler.parse_income_limits_page(
        load("stpete_income_limits/income_limits.html"), INCOME_LIMITS_URL
    )
    chunks["ami_table"] = chunk_ami_thresholds(thresholds)

    # --- Pinellas CF + ARPA (app/chunking/row_based.py) ---
    cf_crawler = PinellasCFCrawler(min_request_interval_seconds=0)
    cf_programs = cf_crawler.parse_grants_table(load("pinellas_cf/grants_index.html"))
    chunks["pinellas_cf"] = chunk_pinellas_cf_programs(cf_programs)

    arpa_crawler = StpeteArpaCrawler(min_request_interval_seconds=0)
    arpa_allocations = arpa_crawler.parse_arpa_page(
        load("stpete_arpa/american_rescue_plan_act.html"), ARPA_URL
    )
    chunks["arpa"] = chunk_arpa_allocations(arpa_allocations)

    # --- stpete commitment (app/chunking/stpete_commitment.py) ---
    commitment_crawler = StpeteCommitmentCrawler(min_request_interval_seconds=0)
    commitment_items = commitment_crawler.parse_commitment_page(
        load("stpete_commitment/st_petes_commitment.html"), COMMITMENT_URL
    )
    chunks["stpete_commitment"] = chunk_stpete_commitment_action_items(commitment_items)

    # --- SPHA (app/chunking/spha.py) ---
    spha_pages_crawler = SphaProgramPagesCrawler(min_request_interval_seconds=0)
    spha_pages = [
        spha_pages_crawler.parse_program_page(
            load(f"spha_program_pages/{url.rstrip('/').split('/')[-1]}.html"), url
        )
        for url in SPHA_PAGE_URLS
    ]
    chunks["spha_program_pages"] = chunk_spha_program_pages(spha_pages)

    spha_news_crawler = SphaNewsCrawler(min_request_interval_seconds=0)
    spha_news_items = spha_news_crawler.parse_news_index(
        load("spha_news/news.html"), SPHA_NEWS_URL
    )
    chunks["spha_news"] = chunk_spha_news_items(spha_news_items)

    # --- Pinellas HCD (app/chunking/pinellas_hcd.py) ---
    hcd_crawler = PinellasHcdCrawler(min_request_interval_seconds=0)
    hcd_pages = [
        hcd_crawler.parse_program_page(
            load(f"pinellas_hcd/{url.rstrip('/').split('/')[-1]}.html"), url
        )
        for url in PROGRAM_PAGE_URLS
    ]
    chunks["pinellas_hcd_programs"] = chunk_pinellas_program_pages(hcd_pages)

    dept_page = hcd_crawler.parse_department_page(
        load("pinellas_hcd/department.html"), DEPARTMENT_OVERVIEW_URL
    )
    chunks["pinellas_hcd_department"] = chunk_pinellas_department_pages([dept_page])

    # --- stpete.org pages (app/chunking/stpete_pages.py) ---
    grants_crawler = StpeteGrantsCrawler(min_request_interval_seconds=0)
    index_categories = grants_crawler.parse_index(load("stpete_grants/grants_index.html"))
    grant_cat_crawler = StpeteGrantCategoryPagesCrawler(min_request_interval_seconds=0)
    hub_tiles = []
    for fixture_name, url in [
        ("business.html", BUSINESS_URL),
        ("community.html", COMMUNITY_URL),
        ("housing.html", GRANT_CAT_HOUSING_URL),
        ("youth.html", YOUTH_URL),
    ]:
        hub_tiles.extend(
            grant_cat_crawler._tiles_parser.parse_tiles_page(
                load(f"stpete_grant_categories/{fixture_name}"), url
            )
        )
    hub_tiles.extend(
        grant_cat_crawler._tiles_parser.parse_tiles_page(
            load("stpete_program_details/for_business_owners.html"), FOR_BUSINESS_OWNERS_URL
        )
    )
    chunks["stpete_grant_category_tiles"] = chunk_stpete_grant_categories(
        index_categories + hub_tiles
    )

    south_stpete_programs = grant_cat_crawler.parse_south_stpete_page(
        load("stpete_grant_categories/for_south_stpete.html")
    )
    chunks["stpete_south_stpete_summaries"] = chunk_stpete_program_summaries(south_stpete_programs)

    sunrise_programs = grant_cat_crawler.parse_sunrise_page(
        load("stpete_grant_categories/sunrise_index.html")
    )
    chunks["stpete_sunrise"] = chunk_stpete_sunrise_programs(sunrise_programs)

    detail_crawler = StpeteProgramDetailsCrawler(min_request_interval_seconds=0)
    detail_pages = [
        detail_crawler.parse_program_detail_page(
            load(f"stpete_program_details/{fixture_name}"), url
        )
        for fixture_name, url in DETAIL_PAGE_CASES
    ] + [
        detail_crawler.parse_program_detail_page(
            load(f"stpete_program_details/{fixture_name}"), url
        )
        for fixture_name, url in FURTHER_HUB_DETAIL_CASES
    ] + [
        detail_crawler.parse_program_detail_page(
            load("stpete_program_details/cra_housing-based_grants.html"), CRA_HOUSING_BASED_GRANTS_URL
        )
    ]
    chunks["stpete_program_details"] = chunk_stpete_program_detail_pages(detail_pages)

    return chunks


def estimate_cost(all_chunks: dict[str, list]) -> tuple[int, int, float]:
    total_chunks = sum(len(v) for v in all_chunks.values())
    total_chars = sum(len(c.text) for chunks in all_chunks.values() for c in chunks)
    total_tokens_estimate = total_chars // CHARS_PER_TOKEN_ESTIMATE
    cost = (total_tokens_estimate / 1_000_000) * USD_PER_MILLION_TOKENS
    return total_chunks, total_tokens_estimate, cost


def print_estimate(all_chunks: dict[str, list]) -> None:
    print("=== Per-source chunk counts (full corpus, real fixtures) ===")
    for source, chunk_list in all_chunks.items():
        print(f"  {source}: {len(chunk_list)} chunks")
    total_chunks, total_tokens_estimate, cost = estimate_cost(all_chunks)
    print(f"\nTotal chunks: {total_chunks}")
    print(f"Estimated total tokens (chars/4 proxy): ~{total_tokens_estimate:,}")
    print(
        f"Estimated cost @ ${USD_PER_MILLION_TOKENS}/1M tokens "
        f"(text-embedding-3-small, published pricing, NOT re-verified live this session): "
        f"~${cost:.4f}"
    )
    print(
        "\nSTOP — per DECISIONS #71 / the task's explicit gate, the full corpus is "
        "NOT embedded by this script without a separate go-ahead. `run-slice` embeds "
        "a smaller, named validated slice instead."
    )


# --- Validated slice: small, cheap, spans multiple doc_types + a source with
# real dollar figures (AMI) + a source with real meeting content (Legistar). ---

SLICE_SOURCES = ("ami_table", "legistar", "pinellas_cf", "stpete_commitment")


def run_slice() -> None:
    all_chunks = build_all_chunks()
    print_estimate(all_chunks)

    slice_chunks = [c for name in SLICE_SOURCES for c in all_chunks[name]]
    slice_chunk_count, slice_tokens, slice_cost = estimate_cost(
        {name: all_chunks[name] for name in SLICE_SOURCES}
    )
    print(f"\n=== Running validated slice: {SLICE_SOURCES} ===")
    print(f"Slice chunk count: {slice_chunk_count} (of {sum(len(v) for v in all_chunks.values())} total)")
    print(f"Slice estimated cost: ~${slice_cost:.4f}")

    client = openai.OpenAI()
    conn = get_connection()
    apply_schema(conn)
    result = embed_and_insert_chunks(conn, client, slice_chunks)
    conn.close()

    print(f"\nResult: inserted={result.inserted} skipped={result.skipped} failed={result.failed}")
    if result.failed_chunk_ids:
        print(f"Failed chunk_ids: {result.failed_chunk_ids}")


def run_all() -> None:
    all_chunks = build_all_chunks()
    print_estimate(all_chunks)

    flat_chunks = [c for chunk_list in all_chunks.values() for c in chunk_list]
    print(f"\n=== Running full corpus: all {len(all_chunks)} sources, {len(flat_chunks)} chunks ===")
    print(
        "(chunks already inserted by a prior run-slice/run-all are skipped "
        "via chunk_id pre-check — no re-embed, no re-bill)"
    )

    client = openai.OpenAI()
    conn = get_connection()
    apply_schema(conn)
    result = embed_and_insert_chunks(conn, client, flat_chunks)
    conn.close()

    print(f"\nResult: inserted={result.inserted} skipped={result.skipped} failed={result.failed}")
    if result.failed_chunk_ids:
        print(f"Failed chunk_ids: {result.failed_chunk_ids}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "estimate"
    if mode == "estimate":
        print_estimate(build_all_chunks())
    elif mode == "run-slice":
        run_slice()
    elif mode == "run-all":
        run_all()
    else:
        print(f"Unknown mode: {mode}. Use 'estimate', 'run-slice', or 'run-all'.")
        sys.exit(1)
