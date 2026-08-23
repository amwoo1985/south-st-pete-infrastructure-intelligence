"""Tests for POST /query's exception -> HTTP status mapping
(app/api/query.py). answer_query() itself is mocked to raise each typed
exception directly (never a real OpenAI call in this file — DECISIONS
#72/#90's mocking discipline) so this exercises only the mapping logic,
not the RAG pipeline it wraps (that's tests/rag/'s job).
"""

from __future__ import annotations

import httpx2
import openai
import pytest

from app.embeddings.client import EmbeddingInputTooLargeError, EmbeddingValidationError
from app.rag.generation import GenerationValidationError, UngroundedAnswerError

_REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")


def _raise(exc: Exception):
    def _fn(*args, **kwargs):
        raise exc

    return _fn


@pytest.mark.parametrize(
    "exc, expected_status",
    [
        (UngroundedAnswerError("no valid citations"), 502),
        (GenerationValidationError("bad JSON from model"), 502),
        (EmbeddingValidationError("malformed embedding shape"), 502),
        (EmbeddingInputTooLargeError("question too long"), 400),
        (
            openai.RateLimitError(
                "rate limited",
                response=httpx2.Response(status_code=429, request=_REQUEST),
                body=None,
            ),
            429,
        ),
        (openai.APITimeoutError(request=_REQUEST), 504),
        (openai.APIConnectionError(request=_REQUEST), 504),
        (
            openai.AuthenticationError(
                "bad key",
                response=httpx2.Response(status_code=401, request=_REQUEST),
                body=None,
            ),
            500,
        ),
        (RuntimeError("something truly unexpected"), 500),
    ],
)
def test_query_exception_maps_to_expected_status(client, monkeypatch, exc, expected_status):
    monkeypatch.setattr("app.api.query.answer_query", _raise(exc))
    resp = client.post("/query", json={"question": "What is the CBA?"})
    assert resp.status_code == expected_status
    body = resp.json()
    assert "detail" in body
    # Never leak the raw internal exception message into the response body
    # — every mapped detail is a static string, distinct from str(exc).
    assert str(exc) not in body["detail"]


def test_query_exception_detail_is_generic_not_raw_exception_text(client, monkeypatch):
    secret_detail = "internal-implementation-detail-that-must-not-leak"
    monkeypatch.setattr(
        "app.api.query.answer_query", _raise(RuntimeError(secret_detail))
    )
    resp = client.post("/query", json={"question": "What is the CBA?"})
    assert resp.status_code == 500
    assert secret_detail not in resp.text


def test_blank_question_is_rejected_with_structured_422(client):
    resp = client.post("/query", json={"question": "   "})
    assert resp.status_code == 422
    body = resp.json()
    assert "detail" in body
    assert isinstance(body["detail"], list)  # field-level detail, not a bare string


def test_empty_question_is_rejected_with_structured_422(client):
    resp = client.post("/query", json={"question": ""})
    assert resp.status_code == 422


def test_oversized_question_is_rejected_with_structured_422(client):
    resp = client.post("/query", json={"question": "x" * 2001})
    assert resp.status_code == 422


def test_successful_query_returns_full_citation_attribution(client, monkeypatch):
    from datetime import date, datetime, timezone

    from app.db.connection import get_connection
    from app.db.schema import CHUNK_COLUMNS
    from app.rag.generation import GenerationResult
    from app.rag.pipeline import AnswerResult
    from app.rag.retrieval import RetrievalResult, RetrievedChunk

    # A real, committed row — the query endpoint's citation lookup
    # (app/api/query.py's _lookup_citations) runs on its OWN connection
    # (a fresh one per request, via get_db_conn), which under Postgres's
    # default READ COMMITTED isolation would never see an uncommitted
    # insert made over a different connection. Committed here, deleted
    # (and committed again) in the finally block below so this test
    # leaves no row behind in the shared dev DB.
    chunk_id = "querytest" + "0" * 23
    values = {
        "chunk_id": chunk_id,
        "doc_type": "uploaded_document",
        "chunk_text": "Some real chunk text about the CBA.",
        "section_label": "Section A",
        "source_url": "upload://testhash",
        "published_date": date(2026, 1, 1),
        "retrieval_timestamp": datetime.now(timezone.utc),
        "start_seconds": None,
        "end_seconds": None,
        "embedding": None,
        "embedding_model": None,
        "embedded_at": None,
    }
    cols = ", ".join(CHUNK_COLUMNS)
    placeholders = ", ".join(["%s"] * len(CHUNK_COLUMNS))
    setup_conn = get_connection()
    try:
        with setup_conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO chunks ({cols}) VALUES ({placeholders}) "
                "ON CONFLICT (chunk_id) DO NOTHING",
                tuple(values[c] for c in CHUNK_COLUMNS),
            )
        setup_conn.commit()
    finally:
        setup_conn.close()

    retrieved = RetrievedChunk(
        chunk_id=chunk_id,
        doc_type="uploaded_document",
        chunk_text=values["chunk_text"],
        section_label="Section A",
        source_url="upload://testhash",
        published_date=date(2026, 1, 1),
        similarity=0.9,
    )
    fake_result = AnswerResult(
        retrieval=RetrievalResult(
            query="What is the CBA?",
            chunks=(retrieved,),
            candidates_considered=1,
            above_threshold=1,
            deduped_out=0,
            context_tokens_estimate=10,
        ),
        generation=GenerationResult(
            answer="It's a community benefits agreement.",
            citations=(chunk_id,),
            not_in_corpus=False,
            invalid_citations_dropped=(),
            model="gpt-4o-mini",
            prompt_tokens=10,
            completion_tokens=5,
        ),
    )

    def _fake_answer_query(conn, client_, query, **kwargs):
        return fake_result

    monkeypatch.setattr("app.api.query.answer_query", _fake_answer_query)

    resp = client.post("/query", json={"question": "What is the CBA?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "It's a community benefits agreement."
    assert body["not_in_corpus"] is False
    assert len(body["citations"]) == 1
    citation = body["citations"][0]
    assert citation["chunk_id"] == chunk_id
    assert citation["doc_type"] == "uploaded_document"
    assert citation["section_label"] == "Section A"
    assert citation["source_url"] == "upload://testhash"
    assert citation["published_date"] == "2026-01-01"

    cleanup_conn = get_connection()
    try:
        with cleanup_conn.cursor() as cur:
            cur.execute("DELETE FROM chunks WHERE chunk_id = %s", (chunk_id,))
        cleanup_conn.commit()
    finally:
        cleanup_conn.close()
