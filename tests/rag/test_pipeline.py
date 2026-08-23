"""Tests for app/rag/pipeline.py's answer_query() composition, against
the real local Postgres (tests/rag/conftest.py) with both OpenAI calls
faked (embeddings AND chat completions — no live network call). This
suite's job is to check the wiring between retrieve() and
generate_answer(), not to re-verify either module's own internal logic
(covered by tests/rag/test_retrieval.py and tests/rag/test_generation.py
respectively).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.db.schema import CHUNK_COLUMNS
from app.rag.pipeline import answer_query

_DIMENSIONS = 1536
QUERY_VECTOR = [1.0] + [0.0] * (_DIMENSIONS - 1)

_INSERT_SQL = (
    f"INSERT INTO chunks ({', '.join(CHUNK_COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(CHUNK_COLUMNS))})"
)


def _insert_chunk(conn, *, chunk_id: str, embedding: list[float], chunk_text: str) -> None:
    now = datetime.now(timezone.utc)
    values = (
        chunk_id,
        "test_doc_type",
        chunk_text,
        "Test Section",
        "https://example.test/pipeline-test",
        None,
        now,
        None,
        None,
        embedding,
        "text-embedding-3-small",
        now,
    )
    with conn.cursor() as cur:
        cur.execute(_INSERT_SQL, values)


def _chat_response(payload: dict):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


def test_answer_query_end_to_end_with_matching_chunk(db_conn, monkeypatch):
    _insert_chunk(
        db_conn,
        chunk_id="rag-pipeline-test-chunk",
        embedding=[0.95] + [0.0] * (_DIMENSIONS - 2) + [
            (1 - 0.95**2) ** 0.5
        ],
        chunk_text="pipeline end-to-end test chunk text",
    )
    monkeypatch.setattr(
        "app.rag.retrieval.get_embedding", lambda client, text: QUERY_VECTOR
    )
    payload = {
        "answer": "The pipeline test chunk says X.",
        "citations": ["rag-pipeline-test-chunk"],
        "not_in_corpus": False,
    }
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_chat_response(payload))

    result = answer_query(
        db_conn, client, "pipeline test query", retrieve_kwargs={"candidate_pool_size": 10}
    )

    assert "rag-pipeline-test-chunk" in {c.chunk_id for c in result.retrieval.chunks}
    assert result.generation.citations == ("rag-pipeline-test-chunk",)
    assert result.generation.not_in_corpus is False
    client.chat.completions.create.assert_called_once()


def test_answer_query_short_circuits_generation_when_nothing_above_threshold(
    db_conn, monkeypatch
):
    monkeypatch.setattr(
        "app.rag.retrieval.get_embedding", lambda client, text: QUERY_VECTOR
    )
    client = MagicMock()
    client.chat.completions.create = MagicMock()

    # threshold > 1.0 is unreachable by any real cosine similarity --
    # deterministic "nothing above threshold" regardless of live corpus
    # content (same trick as tests/rag/test_retrieval.py).
    result = answer_query(
        db_conn,
        client,
        "pipeline not-in-corpus query",
        retrieve_kwargs={"threshold": 1.1, "candidate_pool_size": 5},
    )

    assert result.retrieval.is_empty
    assert result.generation.not_in_corpus is True
    client.chat.completions.create.assert_not_called()
