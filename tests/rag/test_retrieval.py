"""Tests for app/rag/retrieval.py against the real local Postgres (see
tests/rag/conftest.py / tests/db/conftest.py for the transactional-
rollback strategy — DECISIONS #72). The OpenAI side is faked: rather than
mocking `client.embeddings.create` and relying on a fixed fake response
shape (as tests/embeddings/test_client.py does for get_embedding()
itself), these tests monkeypatch `app.rag.retrieval.get_embedding`
directly to return a hand-picked query vector — this suite's job is to
verify threshold/dedup/token-budget SQL-and-Python logic against known
embeddings, not to re-verify get_embedding()'s own retry/validation
behavior (already covered by tests/embeddings/test_client.py).

Vector construction: OpenAI's text-embedding-3-small vectors are unit-
normalized (app/db/schema.py), so cosine similarity between two unit
vectors is just their dot product. `_controlled_vector(similarity,
unique_dim)` builds a 1536-dim unit vector with an exact, chosen
similarity to QUERY_VECTOR ([1.0, 0.0, ...]) by putting the rest of its
unit "mass" into a dimension unique to that one test chunk — this keeps
cross-chunk dot products between DIFFERENT test chunks equal to the
product of their two similarities-to-query (always << the 0.97 dedup
threshold for any two chunks below ~0.98 similarity themselves), so nothing
in this suite accidentally collides as a false "near duplicate" of
another. `_unit_vector(angle_degrees)` (2D, dims 0-1 only) is used
specifically for the one test that needs two chunks close enough to each
other, not just to the query, to legitimately trigger dedup.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.db.schema import CHUNK_COLUMNS
from app.rag.retrieval import RetrievedChunk, retrieve

_DIMENSIONS = 1536
QUERY_VECTOR = [1.0] + [0.0] * (_DIMENSIONS - 1)

_INSERT_SQL = (
    f"INSERT INTO chunks ({', '.join(CHUNK_COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(CHUNK_COLUMNS))})"
)


def _controlled_vector(similarity_to_query: float, unique_dim: int) -> list[float]:
    vec = [0.0] * _DIMENSIONS
    vec[0] = similarity_to_query
    remaining = max(0.0, 1.0 - similarity_to_query**2)
    vec[unique_dim] = math.sqrt(remaining)
    return vec


def _unit_vector(angle_degrees: float) -> list[float]:
    theta = math.radians(angle_degrees)
    vec = [0.0] * _DIMENSIONS
    vec[0] = math.cos(theta)
    vec[1] = math.sin(theta)
    return vec


def _insert_chunk(
    conn,
    *,
    chunk_id: str,
    embedding: list[float],
    chunk_text: str = "synthetic test chunk text for app/rag/retrieval.py",
    doc_type: str = "test_doc_type",
    section_label: str | None = "Test Section",
    source_url: str = "https://example.test/rag-retrieval-test",
    published_date: date | None = date(2026, 1, 1),
) -> None:
    now = datetime.now(timezone.utc)
    values = (
        chunk_id,
        doc_type,
        chunk_text,
        section_label,
        source_url,
        published_date,
        now,
        None,  # start_seconds
        None,  # end_seconds
        embedding,
        "text-embedding-3-small",
        now,
    )
    with conn.cursor() as cur:
        cur.execute(_INSERT_SQL, values)


def _retrieve(db_conn, monkeypatch, query_vector=QUERY_VECTOR, **kwargs):
    monkeypatch.setattr(
        "app.rag.retrieval.get_embedding", lambda client, text: query_vector
    )
    return retrieve(db_conn, MagicMock(), "irrelevant test query text", **kwargs)


# --- Relevance threshold -----------------------------------------------


def test_threshold_keeps_above_and_drops_below(db_conn, monkeypatch):
    _insert_chunk(
        db_conn,
        chunk_id="rag-test-above-threshold",
        embedding=_controlled_vector(0.9, unique_dim=100),
    )
    _insert_chunk(
        db_conn,
        chunk_id="rag-test-below-threshold",
        embedding=_controlled_vector(0.3, unique_dim=101),
    )

    result = _retrieve(db_conn, monkeypatch, candidate_pool_size=10)

    chunk_ids = {c.chunk_id for c in result.chunks}
    assert "rag-test-above-threshold" in chunk_ids
    assert "rag-test-below-threshold" not in chunk_ids

    above = next(c for c in result.chunks if c.chunk_id == "rag-test-above-threshold")
    assert above.similarity == pytest.approx(0.9, abs=1e-6)


def test_impossible_threshold_yields_empty_result(db_conn, monkeypatch):
    _insert_chunk(
        db_conn,
        chunk_id="rag-test-empty-case",
        embedding=_controlled_vector(0.99, unique_dim=102),
    )

    # threshold > 1.0 is unreachable by any real cosine similarity —
    # deterministic way to exercise the "nothing survives" path without
    # depending on the live 342-chunk corpus never coincidentally
    # matching a hand-picked vector above the real 0.5 default.
    result = _retrieve(db_conn, monkeypatch, threshold=1.1, candidate_pool_size=10)

    assert result.is_empty
    assert result.chunks == ()


def test_full_citation_data_passed_through(db_conn, monkeypatch):
    _insert_chunk(
        db_conn,
        chunk_id="rag-test-citation-fields",
        embedding=_controlled_vector(0.87, unique_dim=103),
        chunk_text="citation field passthrough check",
        doc_type="test_doc_type_citation",
        section_label="Section: Citation Check",
        source_url="https://example.test/citation-check",
        published_date=date(2026, 3, 4),
    )

    result = _retrieve(db_conn, monkeypatch, candidate_pool_size=10)

    match = next(c for c in result.chunks if c.chunk_id == "rag-test-citation-fields")
    assert isinstance(match, RetrievedChunk)
    assert match.doc_type == "test_doc_type_citation"
    assert match.chunk_text == "citation field passthrough check"
    assert match.section_label == "Section: Citation Check"
    assert match.source_url == "https://example.test/citation-check"
    assert match.published_date == date(2026, 3, 4)


# --- Deduplication -------------------------------------------------------


def test_dedup_drops_near_duplicate_keeps_higher_ranked(db_conn, monkeypatch):
    # angle ~25.84deg from the query -> cos == 0.9 similarity to query.
    angle_a = math.degrees(math.acos(0.9))
    _insert_chunk(
        db_conn,
        chunk_id="rag-test-dedup-higher-rank",
        embedding=_unit_vector(angle_a),
    )
    # 3 degrees further out: still well above the 0.5 relevance
    # threshold on its own, but cos(3 degrees) ~= 0.9986 similarity to
    # the chunk above -- a real near-duplicate pair under the 0.97 bar.
    _insert_chunk(
        db_conn,
        chunk_id="rag-test-dedup-near-duplicate",
        embedding=_unit_vector(angle_a + 3),
    )

    result = _retrieve(db_conn, monkeypatch, candidate_pool_size=10)

    chunk_ids = {c.chunk_id for c in result.chunks}
    assert "rag-test-dedup-higher-rank" in chunk_ids
    assert "rag-test-dedup-near-duplicate" not in chunk_ids
    assert result.deduped_out >= 1


def test_distinct_but_similar_chunks_both_survive(db_conn, monkeypatch):
    """Mirrors DECISIONS #73's Pinellas HCD finding: two real, distinct,
    similarly-scored (both above threshold) chunks that are NOT a
    near-duplicate pair of each other must both survive into context —
    retrieval doesn't get to unilaterally guess which one the user
    meant."""
    angle_a = math.degrees(math.acos(0.9))
    angle_d = math.degrees(math.acos(0.6))
    # dot(A, D) == cos(angle_d - angle_a) -- well under the 0.97 dedup
    # bar even though both independently pass the relevance threshold.
    assert math.cos(math.radians(angle_d - angle_a)) < 0.97

    _insert_chunk(
        db_conn, chunk_id="rag-test-distinct-a", embedding=_unit_vector(angle_a)
    )
    _insert_chunk(
        db_conn, chunk_id="rag-test-distinct-d", embedding=_unit_vector(angle_d)
    )

    result = _retrieve(db_conn, monkeypatch, candidate_pool_size=10)

    chunk_ids = {c.chunk_id for c in result.chunks}
    assert "rag-test-distinct-a" in chunk_ids
    assert "rag-test-distinct-d" in chunk_ids


# --- Token budget ---------------------------------------------------------


def test_token_budget_stops_including_further_chunks(db_conn, monkeypatch):
    # ~2000 estimated tokens each (8000 chars / 4 chars-per-token).
    big_text = "x" * 8000
    _insert_chunk(
        db_conn,
        chunk_id="rag-test-budget-1",
        embedding=_controlled_vector(0.9, unique_dim=110),
        chunk_text=big_text,
    )
    _insert_chunk(
        db_conn,
        chunk_id="rag-test-budget-2",
        embedding=_controlled_vector(0.85, unique_dim=111),
        chunk_text=big_text,
    )
    _insert_chunk(
        db_conn,
        chunk_id="rag-test-budget-3",
        embedding=_controlled_vector(0.8, unique_dim=112),
        chunk_text=big_text,
    )

    result = _retrieve(
        db_conn, monkeypatch, candidate_pool_size=10, max_context_tokens=3000
    )

    chunk_ids = [c.chunk_id for c in result.chunks]
    # Only the highest-ranked (0.9) chunk fits: 2000 tokens included,
    # adding the second 2000-token chunk would cross the 3000 cap.
    assert chunk_ids == ["rag-test-budget-1"]
    assert result.context_tokens_estimate == 2000


def test_token_budget_always_keeps_at_least_one_chunk_even_if_oversized(
    db_conn, monkeypatch
):
    # ~5000 estimated tokens -- alone already over a 1000-token cap.
    oversized_text = "y" * 20000
    _insert_chunk(
        db_conn,
        chunk_id="rag-test-budget-oversized",
        embedding=_controlled_vector(0.95, unique_dim=113),
        chunk_text=oversized_text,
    )

    result = _retrieve(
        db_conn, monkeypatch, candidate_pool_size=10, max_context_tokens=1000
    )

    chunk_ids = [c.chunk_id for c in result.chunks]
    assert chunk_ids == ["rag-test-budget-oversized"]
    assert result.context_tokens_estimate == 5000


# --- get_embedding integration (not monkeypatched here) -------------------


def test_retrieve_calls_get_embedding_with_client_and_query(db_conn, monkeypatch):
    captured = {}

    def _fake_get_embedding(client, text):
        captured["client"] = client
        captured["text"] = text
        return QUERY_VECTOR

    monkeypatch.setattr("app.rag.retrieval.get_embedding", _fake_get_embedding)
    fake_client = MagicMock()

    retrieve(db_conn, fake_client, "what is the SHIP income limit?", candidate_pool_size=5)

    assert captured["client"] is fake_client
    assert captured["text"] == "what is the SHIP income limit?"
