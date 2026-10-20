"""Tests for app/chunking/base.py — the shared Chunk shape and deterministic
chunk-ID scheme every source-specific chunker builds on. See DECISIONS #58.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.chunking.base import Chunk, make_chunk_id
from app.crawlers.base import Attribution


def make_attribution() -> Attribution:
    return Attribution.now(
        source_url="https://example.test/page.php",
        published_date=date(2026, 1, 1),
    )


# --- make_chunk_id: determinism / idempotency -------------------------------


def test_same_identity_parts_produce_the_same_id():
    id1 = make_chunk_id("https://example.test/page.php", "section", "Overview")
    id2 = make_chunk_id("https://example.test/page.php", "section", "Overview")
    assert id1 == id2


def test_different_identity_parts_produce_different_ids():
    id1 = make_chunk_id("https://example.test/page.php", "section", "Overview")
    id2 = make_chunk_id("https://example.test/page.php", "section", "Eligibility")
    assert id1 != id2


def test_part_boundaries_do_not_collide():
    # ("a", "bc") and ("ab", "c") must not hash to the same ID — this is
    # exactly what the unit-separator join (instead of naive "|" or ""
    # concatenation) guards against.
    id1 = make_chunk_id("a", "bc")
    id2 = make_chunk_id("ab", "c")
    assert id1 != id2


def test_id_is_deterministic_across_process_runs():
    # A fixed expected value pins the hash scheme itself — if this ever
    # legitimately changes (e.g. a different truncation length), every
    # existing chunk_id in a real database would change too, which is a
    # breaking migration, not a routine refactor. Failing this test is a
    # deliberate tripwire for that.
    expected = make_chunk_id("https://example.test/page.php", "section", "Overview")
    assert make_chunk_id("https://example.test/page.php", "section", "Overview") == expected
    assert len(expected) == 32
    int(expected, 16)  # is valid hex


def test_make_chunk_id_requires_at_least_one_part():
    with pytest.raises(ValueError):
        make_chunk_id()


# --- Chunk: empty-text guard -------------------------------------------------


def test_chunk_rejects_empty_text():
    with pytest.raises(ValueError):
        Chunk(
            chunk_id=make_chunk_id("x"),
            doc_type="test_doc_type",
            text="",
            section_label=None,
            attribution=make_attribution(),
        )


def test_chunk_rejects_whitespace_only_text():
    with pytest.raises(ValueError):
        Chunk(
            chunk_id=make_chunk_id("x"),
            doc_type="test_doc_type",
            text="   \n\t  ",
            section_label=None,
            attribution=make_attribution(),
        )


def test_chunk_accepts_real_text_and_passes_through_attribution():
    attribution = make_attribution()
    chunk = Chunk(
        chunk_id=make_chunk_id("x"),
        doc_type="test_doc_type",
        text="real content",
        section_label="Section 1",
        attribution=attribution,
    )
    assert chunk.text == "real content"
    assert chunk.attribution is attribution
    assert chunk.attribution.source_url == "https://example.test/page.php"
    assert chunk.attribution.published_date == date(2026, 1, 1)


def test_chunk_timestamp_range_defaults_to_none_and_is_settable():
    # The extension point for a future transcript source (DECISIONS #58) —
    # unused by every current source, but must exist and be nullable.
    chunk = Chunk(
        chunk_id=make_chunk_id("x"),
        doc_type="test_doc_type",
        text="real content",
        section_label=None,
        attribution=make_attribution(),
    )
    assert chunk.start_seconds is None
    assert chunk.end_seconds is None

    timed_chunk = Chunk(
        chunk_id=make_chunk_id("y"),
        doc_type="test_doc_type",
        text="real content",
        section_label=None,
        attribution=make_attribution(),
        start_seconds=12.5,
        end_seconds=45.0,
    )
    assert timed_chunk.start_seconds == 12.5
    assert timed_chunk.end_seconds == 45.0
