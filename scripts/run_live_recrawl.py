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
crawler.md). (These 8 chunking-module sources fan out into the 13
individually-tracked source segments `_build_source_segments()` returns
below — see DECISIONS #146 — because a few of them, e.g.
stpete_grant_category_tiles, are themselves built from several
independent live fetches merged into one chunk list.)

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

Per-source failure isolation and run tracking (DECISIONS #146): before this,
one source's crawl exception (a `CrawlerStructureError`, a network error,
anything) propagated straight out of a single monolithic
`build_all_chunks_live()` function and killed the ENTIRE script invocation
— every source after the one that failed silently never ran, and nothing
was ever recorded anywhere (stdout included) about which source broke or
what every OTHER source's outcome was. `_build_source_segments()` below
decomposes that monolith into 13 independently-tried closures;
`estimate()` and `run()` each wrap every segment in its own try/except so
one source's failure no longer takes down the rest. In `run` mode, every segment's outcome (success with real
`PipelineResult` counts, or failure with the real exception) is persisted
to the `crawl_runs` table (app/db/crawl_runs.py) — the first persistent,
queryable run history any of these 8 sources has ever had (only Granicus,
app/granicus/schema.py, had this before). Remaining, disclosed gap: a
DB-connection-level failure (`psycopg.OperationalError`/`InterfaceError`)
still aborts the whole invocation rather than being treated as one
source's problem — deliberately, mirroring `embed_and_insert_chunks`'s own
existing philosophy (app/embeddings/pipeline.py) that a systemic failure
must not be misrepresented as N independent per-item failures.

Usage:
    python scripts/run_live_recrawl.py estimate
        Fetches every live page for real (through each crawler's own
        rate-limited, robots.txt-checked, honestly-user-agented `fetch()`),
        parses + chunks it, and prints per-source counts plus a rough
        cost estimate. No embedding API calls, no DB writes — a dry run
        that still proves the live fetch+parse path works end to end. A
        source that fails to crawl is printed as FAILED and skipped; the
        rest of the sources still run (DECISIONS #146).

    python scripts/run_live_recrawl.py run
        Same live fetch+parse+chunk as `estimate`, then embeds+inserts
        each source's chunks against the REAL local Postgres (docker-compose
        `db` service — app.db.connection.get_connection()'s default,
        `127.0.0.1`; this script never points at RDS) using the REAL
        OpenAI API, immediately after that source's own crawl finishes (not
        batched across all sources at the end — see DECISIONS #146). Unlike
        `run_embedding_pipeline.py`'s run-slice/run-all split (whose caution
        was about a large FIRST-time embedding bill), there is no separate
        "slice" mode here: this script is meant to be the executable a
        future recurring trigger runs unattended (once a cadence is
        actually authorized — see the module-level note above), and the
        idempotency property above means a routine re-run's marginal cost
        is small (only genuinely new/changed identity keys get embedded) —
        the original script's "don't accidentally trigger a big first
        bill" hesitancy doesn't apply the same way to a recurring,
        mostly-skip run. `estimate` remains the pre-flight dry-run for
        anyone who wants to sanity-check counts before letting `run` touch
        the DB/API. Every source's outcome — success or failure — is
        persisted to `crawl_runs`, one row per source per invocation.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import openai
import psycopg
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
from app.crawlers.base import DEFAULT_MIN_REQUEST_INTERVAL_SECONDS, ScopeViolationError
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
from app.db.crawl_runs import apply_crawl_runs_schema, record_crawl_run
from app.db.schema import apply_schema
from app.embeddings.pipeline import embed_and_insert_chunks

load_dotenv()
logger = logging.getLogger("scripts.run_live_recrawl")

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
# source segment below, and between the individual live fetches WITHIN a
# segment that spans more than one call (DECISIONS #146) — cheap (well
# under a minute total across all sources) and removes the doubt without
# touching shared crawler code.
def _pause_between_sources() -> None:
    time.sleep(DEFAULT_MIN_REQUEST_INTERVAL_SECONDS)


@dataclass(frozen=True)
class SourceSegment:
    """One independently-tried unit of crawl+chunk work. `name` matches
    the source_name recorded in `crawl_runs` (DECISIONS #146) and printed
    in `estimate` output — keep the two in sync."""

    name: str
    crawl: Callable[[], list]


def _build_source_segments() -> list[SourceSegment]:
    """Builds and returns the 13 SourceSegments. Every crawler class is instantiated
    here, up front, unconditionally — `BaseCrawler.__init__` does no
    network I/O (see app/crawlers/base.py), so instantiating never fails
    the way a live `.crawl()` call can. That's what lets segments later in
    this list (e.g. stpete_south_stpete_summaries, which reuses
    `grant_cat_crawler`) still run normally even if an EARLIER segment
    that happened to use the same crawler instance for a different page
    (stpete_grant_category_tiles) failed — the instances themselves are
    never in a partially-constructed state, only individual `.crawl*()`
    calls can raise (DECISIONS #146)."""
    legistar_crawler = LegistarCrawler()
    income_crawler = StpeteIncomeLimitsCrawler()
    cf_crawler = PinellasCFCrawler()
    arpa_crawler = StpeteArpaCrawler()
    commitment_crawler = StpeteCommitmentCrawler()
    spha_pages_crawler = SphaProgramPagesCrawler()
    spha_news_crawler = SphaNewsCrawler()
    hcd_crawler = PinellasHcdCrawler()
    grants_crawler = StpeteGrantsCrawler()
    grant_cat_crawler = StpeteGrantCategoryPagesCrawler()
    details_crawler = StpeteProgramDetailsCrawler()

    def _crawl_legistar() -> list:
        # date_range="This Month" (the default) is the crawler's own
        # documented normal steady-state behavior for finding new/upcoming
        # meetings — exactly what a weekly recrawl wants, not a past-date
        # backfill.
        meetings = legistar_crawler.crawl()
        return chunk_legistar_meetings(meetings)

    def _crawl_ami_table() -> list:
        thresholds = income_crawler.crawl()
        return chunk_ami_thresholds(thresholds)

    def _crawl_pinellas_cf() -> list:
        cf_programs = cf_crawler.crawl()
        return chunk_pinellas_cf_programs(cf_programs)

    def _crawl_arpa() -> list:
        arpa_allocations = arpa_crawler.crawl()
        return chunk_arpa_allocations(arpa_allocations)

    def _crawl_stpete_commitment() -> list:
        commitment_items = commitment_crawler.crawl()
        return chunk_stpete_commitment_action_items(commitment_items)

    def _crawl_spha_program_pages() -> list:
        spha_pages = spha_pages_crawler.crawl()
        return chunk_spha_program_pages(list(spha_pages.values()))

    def _crawl_spha_news() -> list:
        spha_news_items = spha_news_crawler.crawl()
        return chunk_spha_news_items(spha_news_items)

    def _crawl_pinellas_hcd_programs() -> list:
        hcd_pages = hcd_crawler.crawl_program_pages()
        return chunk_pinellas_program_pages(list(hcd_pages.values()))

    def _crawl_pinellas_hcd_department() -> list:
        dept_pages = hcd_crawler.crawl_department_page()
        return chunk_pinellas_department_pages(list(dept_pages.values()))

    def _crawl_stpete_grant_category_tiles() -> list:
        # stpete.org grant category tiles: index + 4 hub pages + the
        # for_business_owners.php further-hub page — 3 separate live
        # fetches merged into one chunk list/one tracked source segment.
        index_categories = grants_crawler.crawl()
        _pause_between_sources()

        hub_pages = grant_cat_crawler.crawl_hub_pages()
        hub_tiles: list = []
        for tiles in hub_pages.values():
            hub_tiles.extend(tiles)
        _pause_between_sources()

        hub_tiles.extend(details_crawler.crawl_further_hub_page())
        return chunk_stpete_grant_categories(index_categories + hub_tiles)

    def _crawl_stpete_south_stpete_summaries() -> list:
        south_stpete_programs = grant_cat_crawler.crawl_south_stpete_page()
        return chunk_stpete_program_summaries(south_stpete_programs)

    def _crawl_stpete_sunrise() -> list:
        sunrise_programs = grant_cat_crawler.crawl_sunrise_page()
        return chunk_stpete_sunrise_programs(sunrise_programs)

    def _crawl_stpete_program_details() -> list:
        # stpete.org program detail pages: 22 DECISIONS #35 pages + 3
        # further-hub pages + 1 DECISIONS #41 page — 3 separate live
        # fetches merged into one chunk list/one tracked source segment.
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
        return chunk_stpete_program_detail_pages(all_detail_pages)

    return [
        SourceSegment("legistar", _crawl_legistar),
        SourceSegment("ami_table", _crawl_ami_table),
        SourceSegment("pinellas_cf", _crawl_pinellas_cf),
        SourceSegment("arpa", _crawl_arpa),
        SourceSegment("stpete_commitment", _crawl_stpete_commitment),
        SourceSegment("spha_program_pages", _crawl_spha_program_pages),
        SourceSegment("spha_news", _crawl_spha_news),
        SourceSegment("pinellas_hcd_programs", _crawl_pinellas_hcd_programs),
        SourceSegment("pinellas_hcd_department", _crawl_pinellas_hcd_department),
        SourceSegment("stpete_grant_category_tiles", _crawl_stpete_grant_category_tiles),
        SourceSegment("stpete_south_stpete_summaries", _crawl_stpete_south_stpete_summaries),
        SourceSegment("stpete_sunrise", _crawl_stpete_sunrise),
        SourceSegment("stpete_program_details", _crawl_stpete_program_details),
    ]


def estimate_cost(all_chunks: dict[str, list]) -> tuple[int, int, float]:
    total_chunks = sum(len(v) for v in all_chunks.values())
    total_chars = sum(len(c.text) for chunks in all_chunks.values() for c in chunks)
    total_tokens_estimate = total_chars // CHARS_PER_TOKEN_ESTIMATE
    cost = (total_tokens_estimate / 1_000_000) * USD_PER_MILLION_TOKENS
    return total_chunks, total_tokens_estimate, cost


def print_estimate(all_chunks: dict[str, list], failed_sources: list[str]) -> None:
    print("=== Per-source chunk counts (live fetch, real network calls) ===")
    for source, chunk_list in all_chunks.items():
        print(f"  {source}: {len(chunk_list)} chunks")
    if failed_sources:
        print("\n=== Sources that FAILED to crawl (skipped, not counted above) ===")
        for source in failed_sources:
            print(f"  {source}: FAILED — see error above")
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


def estimate() -> None:
    segments = _build_source_segments()
    chunks: dict[str, list] = {}
    failed_sources: list[str] = []

    for segment in segments:
        try:
            chunks[segment.name] = segment.crawl()
            print(f"{segment.name}: {len(chunks[segment.name])} chunks")
        except ScopeViolationError:
            # Never catch-and-continue a scope violation (app/crawlers/
            # base.py's own documented contract: "either a bug or an
            # undocumented scope expansion") — treating it as ordinary
            # per-source flakiness would defeat the whole point of that
            # exception class. Abort loudly instead of downgrading it.
            raise
        except Exception as exc:
            failed_sources.append(segment.name)
            print(f"{segment.name}: FAILED — {type(exc).__name__}: {exc}")
            logger.error("[estimate] %s crawl failed: %s", segment.name, exc)
        _pause_between_sources()

    print()
    print_estimate(chunks, failed_sources)


def run() -> None:
    run_id = str(uuid.uuid4())
    segments = _build_source_segments()

    print(f"=== run_id={run_id}: {len(segments)} source segments ===")

    client = openai.OpenAI()
    conn = get_connection()
    apply_schema(conn)
    apply_crawl_runs_schema(conn)

    totals_inserted = totals_skipped = totals_failed_chunks = 0
    succeeded_sources = 0
    failed_sources = 0

    try:
        for segment in segments:
            started_at = datetime.now(timezone.utc)
            try:
                chunk_list = segment.crawl()
            except (psycopg.OperationalError, psycopg.InterfaceError):
                # Defensive guard, not currently reachable: no
                # `_crawl_*` closure in `_build_source_segments()` touches
                # `conn` today, so a real psycopg connection error can't
                # actually originate inside `segment.crawl()`. Kept
                # explicit anyway so that if a future segment ever does
                # gain DB access, a systemic connection failure there
                # still aborts the whole invocation rather than being
                # recorded as that one source's problem (same philosophy
                # as embed_and_insert_chunks, app/embeddings/pipeline.py).
                raise
            except ScopeViolationError:
                # Never catch-and-continue a scope violation
                # (app/crawlers/base.py's own documented contract) — abort
                # loudly rather than record it as ordinary per-source
                # flakiness. See the matching guard in estimate() above.
                raise
            except Exception as exc:
                finished_at = datetime.now(timezone.utc)
                error_message = f"{type(exc).__name__}: {exc}"
                print(f"\n{segment.name}: CRAWL FAILED — {error_message}")
                logger.error("[run] %s crawl failed: %s", segment.name, exc)
                record_crawl_run(
                    conn,
                    run_id=run_id,
                    source_name=segment.name,
                    started_at=started_at,
                    finished_at=finished_at,
                    outcome="failed",
                    error_message=error_message,
                )
                failed_sources += 1
                _pause_between_sources()
                continue

            # No wrapping try/except here: embed_and_insert_chunks
            # (app/embeddings/pipeline.py) already only lets
            # psycopg.OperationalError/InterfaceError escape — every other
            # per-chunk failure is caught internally and folded into
            # PipelineResult.failed. Those two exception types propagate
            # naturally out of this function and out of `run()` (there is
            # no other handler for them below), aborting the whole
            # invocation via the `finally: conn.close()` — the same
            # systemic-failure-must-not-look-like-per-source-noise outcome
            # as before, without a redundant catch-then-immediately-raise.
            result = embed_and_insert_chunks(conn, client, chunk_list)

            finished_at = datetime.now(timezone.utc)
            print(
                f"\n{segment.name}: {len(chunk_list)} chunks — "
                f"inserted={result.inserted} skipped={result.skipped} failed={result.failed}"
            )
            if result.failed_chunk_ids:
                print(f"  failed chunk_ids: {result.failed_chunk_ids}")

            # A source only counts as outcome='success' if the crawl step
            # AND at least some of its chunks actually landed or were
            # legitimately already there (inserted or skipped > 0), or the
            # source genuinely produced zero chunks this run (nothing to
            # fail). If chunks were produced but EVERY one of them failed
            # to embed/insert (e.g. a broken OpenAI key, a schema
            # mismatch), that is a real, total failure for this source —
            # recording it as 'success' just because the crawl step itself
            # didn't raise would recreate exactly the silent-looks-fine
            # blind spot this table exists to close (crawler-review
            # pre-commit finding, DECISIONS #146).
            total_attempted = result.total
            all_chunks_failed = total_attempted > 0 and result.inserted == 0 and result.skipped == 0
            if all_chunks_failed:
                outcome = "failed"
                error_message = (
                    f"crawl succeeded ({len(chunk_list)} chunks) but all "
                    f"{result.failed} chunk(s) failed to embed/insert — see "
                    f"failed_chunk_ids in logs above"
                )
                failed_sources += 1
            else:
                outcome = "success"
                error_message = None
                succeeded_sources += 1

            record_crawl_run(
                conn,
                run_id=run_id,
                source_name=segment.name,
                started_at=started_at,
                finished_at=finished_at,
                outcome=outcome,
                inserted=result.inserted,
                skipped=result.skipped,
                chunks_failed=result.failed,
                error_message=error_message,
            )
            totals_inserted += result.inserted
            totals_skipped += result.skipped
            totals_failed_chunks += result.failed
            _pause_between_sources()
    finally:
        conn.close()

    print(
        f"\n=== run_id={run_id} done: {succeeded_sources} source(s) crawled OK, "
        f"{failed_sources} source(s) failed to crawl ===\n"
        f"Totals across succeeded sources: inserted={totals_inserted} "
        f"skipped={totals_skipped} chunks_failed={totals_failed_chunks}"
    )


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "estimate"
    if mode == "estimate":
        estimate()
    elif mode == "run":
        run()
    else:
        print(f"Unknown mode: {mode}. Use 'estimate' or 'run'.")
        sys.exit(1)
