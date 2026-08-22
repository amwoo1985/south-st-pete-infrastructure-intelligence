"""Tests for app/chunking/stpete_commitment.py.

Uses the real recorded fixture run through StpeteCommitmentCrawler's own
real parse method (parse_commitment_page()) - the same real dataclass
instances a live crawl would produce, never hand-built stand-ins.
"""

from __future__ import annotations

from pathlib import Path

from app.chunking.stpete_commitment import (
    DOC_TYPE_COMMITMENT_ACTION_ITEM,
    chunk_stpete_commitment_action_item,
    chunk_stpete_commitment_action_items,
)
from app.crawlers.stpete_commitment import COMMITMENT_URL, StpeteCommitmentCrawler

FIXTURES_ROOT = Path(__file__).parent.parent / "fixtures"


def load(rel_path: str) -> str:
    return (FIXTURES_ROOT / rel_path).read_text(encoding="utf-8")


def load_real_items():
    crawler = StpeteCommitmentCrawler(min_request_interval_seconds=0)
    return crawler.parse_commitment_page(load("stpete_commitment/st_petes_commitment.html"), COMMITMENT_URL)


def test_chunks_one_per_action_item():
    items = load_real_items()
    assert len(items) == 16  # 10 Building Sector + 6 Transportation Sector, confirmed live

    chunks = chunk_stpete_commitment_action_items(items)
    assert len(chunks) == 16
    assert all(c.doc_type == DOC_TYPE_COMMITMENT_ACTION_ITEM for c in chunks)


def test_chunk_text_includes_full_item_text_and_sector():
    items = load_real_items()
    chunks = chunk_stpete_commitment_action_items(items)
    for item, chunk in zip(items, chunks):
        assert item.text in chunk.text
        assert item.sector in chunk.text


def test_duke_energy_moonshot_item_is_chunked():
    # The exact item DECISIONS #46/#47 named this page for - confirm it
    # survives chunking, not just crawling.
    items = load_real_items()
    chunks = chunk_stpete_commitment_action_items(items)
    duke_chunks = [c for c in chunks if "Duke Energy community solar" in c.text]
    assert len(duke_chunks) == 1
    assert "Moonshot" in duke_chunks[0].text


def test_chunk_ids_are_deterministic_and_unique():
    items = load_real_items()
    ids_1 = [c.chunk_id for c in chunk_stpete_commitment_action_items(items)]
    ids_2 = [c.chunk_id for c in chunk_stpete_commitment_action_items(items)]
    assert ids_1 == ids_2
    assert len(ids_1) == len(set(ids_1))


def test_ordinal_within_sector_resets_per_sector_without_id_collision():
    # Building Sector item 1 and Transportation Sector item 1 share an
    # ordinal but must not collide - sector is part of the positional
    # identity too (see module docstring's identity-field discussion).
    items = load_real_items()
    building = [i for i in items if i.sector == "Building Sector"]
    transportation = [i for i in items if i.sector == "Transportation Sector"]
    assert len(building) == 10
    assert len(transportation) == 6

    building_first_id = chunk_stpete_commitment_action_item(building[0], 1).chunk_id
    transportation_first_id = chunk_stpete_commitment_action_item(transportation[0], 1).chunk_id
    assert building_first_id != transportation_first_id


def test_attribution_passthrough_and_no_published_date():
    # No per-item date exists on this page (module docstring / DECISIONS
    # #47) - nullable, not a guessed sentinel.
    items = load_real_items()
    chunks = chunk_stpete_commitment_action_items(items)
    for item, chunk in zip(items, chunks):
        assert chunk.attribution is item.attribution
        assert chunk.attribution.published_date is None
        assert chunk.attribution.source_url == COMMITMENT_URL
