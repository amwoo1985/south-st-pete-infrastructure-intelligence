"""Live-fetch twin of `scripts/run_embedding_pipeline.py` — the thing a
scheduled recrawl trigger (deploy-infra's EventBridge mechanism, built in
parallel with this script) would call. NOTE: the recrawl CADENCE itself
(weekly, or any other interval) is NOT yet authorized by a DECISIONS.md
entry — DECISIONS #124 explicitly leaves "recrawl cadence not yet decided"
open, and this script does not settle that. This script is only the
executable that a cadence decision, once logged, would point a scheduler
at — do not wire up an actual recurring trigger against it without that
DECISIONS entry existing first.

Why this script exists (audit finding, 2026-08-23): `run_embedding_pipeline.py`
builds every chunk this codebase can produce entirely from FROZEN fixtures
under `tests/fixtures/` — its own docstring says so ("Pure parsing — no
network, no API calls"). A weekly schedule that re-runs that script would
re-embed the exact same frozen snapshot forever, giving false confidence
that content is being kept current when it never actually re-fetches the
live web. This script closes that gap: it calls each crawler's own real,
already-built, network-calling entrypoint (`.crawl()` or the specific
`crawl_*` methods `scripts/run_embedding_pipeline.py`'s `build_all_chunks()`
already documents the fixture/URL mapping for) instead of loading a fixture
file.

Source set (closed, DECISIONS #11 + #48): the exact 8 chunk-producing
sources `run_embedding_pipeline.py`'s `build_all_chunks()` already covers —
legistar, ami_table (stpete income limits), pinellas_cf, arpa (stpete),
stpete_commitment, spha (news + program pages), pinellas_hcd (programs +
department), and stpete_pages (grant categories, south st pete summaries,
sunrise, program details). Nothing beyond that set is added here — adding a
9th source requires its own new DECISIONS.md entry first (.claude/rules/
crawler.md).

Explicitly NOT touched by this script (do not extend it to cover these
without a new DECISIONS entry / explicit authorization):
- `app/crawlers/stpete_council_votes.py` (Accountability Tracker) — not
  part of the RAG chunk/embed pipeline, no chunker feeds it into
  `embed_and_insert_chunks`.
- Anything under `app/granicus/`, and `stpete.granicus.com` itself —
  `stpete.granicus.com`'s robots.txt is a real, published `Disallow: /`
  (DECISIONS #77/#79); `ALLOWED_SOURCE_HOSTS` in `app/crawlers/base.py`
  doesn't even list that host, so any code path that tried would hit
  `ScopeViolationError` immediately. Granicus meeting discovery/audio/
  transcription is a separate, already-decided sub-pipeline, out of scope
  for a web-page recrawl script.

Idempotency (the load-bearing property for a weekly re-run being cheap —
.claude/rules/data.md): every one of these 8 sources' chunkers derives
`chunk_id` from stable IDENTITY fields (source URL + section heading / row
key / item label / ordinal) via `app.chunking.base.make_chunk_id` — NEVER
from the chunk's own body text (verified by reading every `make_chunk_id(...)`
call site in `app/chunking/*.py` this session). Combined with
`embed_and_insert_chunk`'s chunk_id pre-check (app/embeddings/pipeline.py),
this means: re-running this script weekly against unchanged live pages costs
nothing extra (same chunk_ids, skipped, no embedding API call) — only
genuinely NEW identity keys (a new agenda item, a new program page section,
a new SPHA news item) produce new embedding spend.

**Known, real, NOT-fixed-here gap, same shape as DECISIONS #118's Granicus
finding, but broader: it applies to all 8 of these sources, not just
Granicus.** Because chunk_id is identity-derived and never content-derived,
a live page whose BODY TEXT changes while its identity fields stay the same
(e.g. stpete.org edits a program description's wording, or corrects a
dollar figure, without renaming the section heading or program) produces
the SAME chunk_id as before. `embed_and_insert_chunk`'s pre-check sees that
chunk_id already has a row and returns "skipped" BEFORE ever reaching the
`INSERT ... ON CONFLICT DO UPDATE` in `app/embeddings/pipeline.py` — so the
corrected/updated live content is silently never re-embedded, and a
citation would keep pointing at now-stale text. This is not a bug
introduced by this script; it's an existing, documented property of
`make_chunk_id`'s design (deliberately content-blind, per its own
docstring) surfacing for the first time in a code path that actually
re-fetches live content on a schedule. Flagged here plainly, same as
DECISIONS #118 flagged it for the Granicus transcript chunker specifically
— fixing it (e.g. hashing a content fingerprint into a *secondary* staleness
check, separate from the identity-based chunk_id used for dedup) is
out of scope for this script and would need its own DECISIONS entry.

Usage:
    python scripts/run_live_recrawl.py estimate
        Fetches every live page for real (through each crawler's own
        rate-limited, robots.txt-checked, honestly-user-agented `fetch()`),
        parses + chunks it, and prints per-source counts plus a rough
        cost estimate. No embedding API calls, no DB writes — a dry run
        that still proves the live fetch+parse path works end to end.

    python scripts/run_live_recrawl.py run
        Same live fetch+parse+chunk as `estimate`, then embeds+inserts
        every chunk against the REAL local Postgres (docker-compose `db`
        service — app.db.connection.get_connection()'s default,
        `127.0.0.1`; this script never points at RDS) using the REAL
        OpenAI API. Unlike `run_embedding_pipeline.py`'s run-slice/run-all
        split (whose caution was about a large FIRST-time embedding bill),
        there is no separate "slice" mode here: this script is meant to be
        the executable a future recurring trigger runs unattended (once a
        cadence is actually authorized — see the module-level note above),
        and the idempotency property above means a routine re-run's
        marginal cost is small
        (only genuinely new/changed identity keys get embedded) — the
        original script's "don't accidentally trigger a big first bill"
        hesitancy doesn't apply the same way to a recurring, mostly-skip
        run. `estimate` remains the pre-flight dry-run for anyone who wants
        to sanity-check counts before letting `run` touch the DB/API.
"""

from __future__ import annotations

import sys
import time
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
from app.crawlers.base import DEFAULT_MIN_REQUEST_INTERVAL_SECONDS
from app.crawlers.legistar import LegistarCrawler
from app.crawlers.pinellas_cf import PinellasCFCrawler
from app.crawlers.pinellas_hcd import PinellasHcdCrawler
from app.crawlers.spha_news import SphaNewsCrawler
from app.crawlers.spha_program_pages import SphaProgramPagesCrawler
from app.crawlers.stpete_arpa import StpeteArpaCrawler
from app.crawlers.stpete_commitment import StpeteCommitmentCrawler
from app.crawlers.stpete_grant_categories import StpeteGrantCategoryPagesCrawler
from app.crawlers.stpete_grants import StpeteGrantsCrawler
from app.crawlers.stpete_income_limits import StpeteIncomeLimitsCrawler
from app.crawlers.stpete_program_details import StpeteProgramDetailsCrawler
from app.db.connection import get_connection
from app.db.schema import apply_schema
from app.embeddings.pipeline import embed_and_insert_chunks

load_dotenv()

# Same published, NOT re-verified-live-this-session pricing note as
# run_embedding_pipeline.py — see that script for the caveat.
USD_PER_MILLION_TOKENS = 0.02
CHARS_PER_TOKEN_ESTIMATE = 4

# `BaseCrawler`'s RateLimiter is per-crawler-INSTANCE, not shared across the
# several different crawler classes this script must instantiate serially
# against overlapping hosts (stpete.org/www.stpete.org in particular:
# StpeteGrantsCrawler, StpeteGrantCategoryPagesCrawler,
# StpeteProgramDetailsCrawler, StpeteCommitmentCrawler, StpeteArpaCrawler,
# StpeteIncomeLimitsCrawler all fetch that same host, each with its own
# fresh, empty rate-limiter state). Left as-is, switching from one crawler
# instance to the next could fire two back-to-back requests to the same
# host with no enforced gap between them — a real, if narrow, gap in
# per-host politeness that the existing per-instance RateLimiter design
# doesn't cover across instances. Fixing that properly belongs in
# `app/crawlers/base.py` (a shared, host-keyed limiter), which is out of
# this script's scope (script-only, not touching app code). As a script-
# level mitigation, a defensive pause of the same real interval
# (DEFAULT_MIN_REQUEST_INTERVAL_SECONDS, 2.0s) is inserted between every
# source segment below — cheap (well under a minute total across all
# sources) and removes the doubt without touching shared crawler code.
def _pause_between_sources() -> None:
    time.sleep(DEFAULT_MIN_REQUEST_INTERVAL_SECONDS)


def build_all_chunks_live() -> dict[str, list]:
    """Returns {source_name: [Chunk, ...]} for every one of the 8 chunking
    modules, run against a REAL live fetch of the actual current page for
    every URL `run_embedding_pipeline.py`'s `build_all_chunks()` documents
    (that function's fixture/URL mapping IS the closed, authorized source
    list this mirrors — see DECISIONS #11/#48). Real network calls, real
    robots.txt checks, real rate limiting, real fail-loud structure errors —
    nothing here is a stand-in."""
    chunks: dict[str, list] = {}

    # --- Legistar ---
    # date_range="This Month" (the default) is the crawler's own documented
    # normal steady-state behavior for finding new/upcoming meetings —
    # exactly what a weekly recrawl wants, not a past-date backfill.
    legistar_crawler = LegistarCrawler()
    meetings = legistar_crawler.crawl()
    chunks["legistar"] = chunk_legistar_meetings(meetings)
    _pause_between_sources()

    # --- AMI table (stpete.org income limits) ---
    income_crawler = StpeteIncomeLimitsCrawler()
    thresholds = income_crawler.crawl()
    chunks["ami_table"] = chunk_ami_thresholds(thresholds)
    _pause_between_sources()

    # --- Pinellas CF ---
    cf_crawler = PinellasCFCrawler()
    cf_programs = cf_crawler.crawl()
    chunks["pinellas_cf"] = chunk_pinellas_cf_programs(cf_programs)
    _pause_between_sources()

    # --- stpete.org ARPA allocations ---
    arpa_crawler = StpeteArpaCrawler()
    arpa_allocations = arpa_crawler.crawl()
    chunks["arpa"] = chunk_arpa_allocations(arpa_allocations)
    _pause_between_sources()

    # --- stpete.org commitment action items ---
    commitment_crawler = StpeteCommitmentCrawler()
    commitment_items = commitment_crawler.crawl()
    chunks["stpete_commitment"] = chunk_stpete_commitment_action_items(commitment_items)
    _pause_between_sources()

    # --- SPHA (stpeteha.org) program pages + news ---
    spha_pages_crawler = SphaProgramPagesCrawler()
    spha_pages = spha_pages_crawler.crawl()
    chunks["spha_program_pages"] = chunk_spha_program_pages(list(spha_pages.values()))
    _pause_between_sources()

    spha_news_crawler = SphaNewsCrawler()
    spha_news_items = spha_news_crawler.crawl()
    chunks["spha_news"] = chunk_spha_news_items(spha_news_items)
    _pause_between_sources()

    # --- Pinellas HCD (pinellas.gov) program pages + department overview ---
    hcd_crawler = PinellasHcdCrawler()
    hcd_pages = hcd_crawler.crawl_program_pages()
    chunks["pinellas_hcd_programs"] = chunk_pinellas_program_pages(list(hcd_pages.values()))
    _pause_between_sources()

    dept_pages = hcd_crawler.crawl_department_page()
    chunks["pinellas_hcd_department"] = chunk_pinellas_department_pages(list(dept_pages.values()))
    _pause_between_sources()

    # --- stpete.org grant category tiles: index + 4 hub pages + the
    # for_business_owners.php further-hub page ---
    grants_crawler = StpeteGrantsCrawler()
    index_categories = grants_crawler.crawl()
    _pause_between_sources()

    grant_cat_crawler = StpeteGrantCategoryPagesCrawler()
    hub_pages = grant_cat_crawler.crawl_hub_pages()
    hub_tiles: list = []
    for tiles in hub_pages.values():
        hub_tiles.extend(tiles)
    _pause_between_sources()

    details_crawler = StpeteProgramDetailsCrawler()
    hub_tiles.extend(details_crawler.crawl_further_hub_page())
    chunks["stpete_grant_category_tiles"] = chunk_stpete_grant_categories(
        index_categories + hub_tiles
    )
    _pause_between_sources()

    south_stpete_programs = grant_cat_crawler.crawl_south_stpete_page()
    chunks["stpete_south_stpete_summaries"] = chunk_stpete_program_summaries(south_stpete_programs)
    _pause_between_sources()

    sunrise_programs = grant_cat_crawler.crawl_sunrise_page()
    chunks["stpete_sunrise"] = chunk_stpete_sunrise_programs(sunrise_programs)
    _pause_between_sources()

    # --- stpete.org program detail pages: 22 DECISIONS #35 pages + 3
    # further-hub pages + 1 DECISIONS #41 page ---
    detail_pages = details_crawler.crawl_detail_pages()
    _pause_between_sources()
    further_hub_detail_pages = details_crawler.crawl_further_hub_detail_pages()
    _pause_between_sources()
    decisions_41_pages = details_crawler.crawl_decisions_41_page()

    all_detail_pages = (
        list(detail_pages.values())
        + list(further_hub_detail_pages.values())
        + list(decisions_41_pages.values())
    )
    chunks["stpete_program_details"] = chunk_stpete_program_detail_pages(all_detail_pages)

    return chunks


def estimate_cost(all_chunks: dict[str, list]) -> tuple[int, int, float]:
    total_chunks = sum(len(v) for v in all_chunks.values())
    total_chars = sum(len(c.text) for chunks in all_chunks.values() for c in chunks)
    total_tokens_estimate = total_chars // CHARS_PER_TOKEN_ESTIMATE
    cost = (total_tokens_estimate / 1_000_000) * USD_PER_MILLION_TOKENS
    return total_chunks, total_tokens_estimate, cost


def print_estimate(all_chunks: dict[str, list]) -> None:
    print("=== Per-source chunk counts (live fetch, real network calls) ===")
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
        "\nNote: in practice this cost only applies to chunk_ids not already "
        "in the `chunks` table — `run`'s embed step skips anything already "
        "embedded (see module docstring's idempotency section)."
    )


def run() -> None:
    all_chunks = build_all_chunks_live()
    print_estimate(all_chunks)

    flat_chunks = [c for chunk_list in all_chunks.values() for c in chunk_list]
    print(f"\n=== Embedding + inserting: {len(all_chunks)} sources, {len(flat_chunks)} chunks ===")
    print(
        "(chunks whose chunk_id already has a row in `chunks` are skipped — "
        "no re-embed, no re-bill; see module docstring's idempotency note "
        "for the one known content-change gap this does NOT catch)"
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
        print_estimate(build_all_chunks_live())
    elif mode == "run":
        run()
    else:
        print(f"Unknown mode: {mode}. Use 'estimate' or 'run'.")
        sys.exit(1)
