"""Tests for app/embeddings/pipeline.py against the real local Postgres
(see tests/db/conftest.py for the transactional-rollback strategy). The
OpenAI side is still faked (a MagicMock client, same approach as
tests/embeddings/test_client.py) — this suite's job is to verify the
DB/idempotency/partial-failure behavior, not to re-verify the embeddings
client itself.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.chunking.base import Chunk, make_chunk_id
from app.crawlers.base import Attribution
from app.embeddings.client import EMBEDDING_DIMENSIONS
from app.embeddings.pipeline import (
    _insert_chunk_row,
    chunk_already_embedded,
    embed_and_insert_chunk,
    embed_and_insert_chunks,
)


def _valid_embedding(fill: float = 0.1) -> list[float]:
    return [fill] * EMBEDDING_DIMENSIONS


def _fake_client(**embeddings_create_kwargs) -> MagicMock:
    client = MagicMock()
    client.embeddings.create = MagicMock(**embeddings_create_kwargs)
    return client


def _fake_response(embedding: list) -> SimpleNamespace:
    return SimpleNamespace(data=[SimpleNamespace(embedding=embedding)])


def _make_chunk(identity_suffix: str, text: str = "some real chunk text") -> Chunk:
    source_url = f"https://www.stpete.org/test/{identity_suffix}"
    return Chunk(
        chunk_id=make_chunk_id(source_url, identity_suffix),
        doc_type="test_doc_type",
        text=text,
        section_label=f"Section {identity_suffix}",
        attribution=Attribution(
            source_url=source_url,
            retrieval_timestamp=datetime(2026, 8, 21, tzinfo=timezone.utc),
            published_date=date(2026, 8, 13),
        ),
    )


# --- Pre-check / idempotency ------------------------------------------------


def test_chunk_already_embedded_false_for_unseen_chunk_id(db_conn):
    assert chunk_already_embedded(db_conn, "does-not-exist") is False


def test_embed_and_insert_chunk_skips_and_makes_no_api_call_when_chunk_id_exists(db_conn):
    chunk = _make_chunk("skip-test")
    client = _fake_client(return_value=_fake_response(_valid_embedding()))

    first = embed_and_insert_chunk(db_conn, client, chunk, commit=False)
    assert first == "inserted"
    assert client.embeddings.create.call_count == 1

    second = embed_and_insert_chunk(db_conn, client, chunk, commit=False)
    assert second == "skipped"
    # No second API call — the whole point of the pre-check per
    # .claude/rules/deploy.md's idempotency-key-before-external-write rule.
    assert client.embeddings.create.call_count == 1


def test_advisory_lock_serializes_concurrent_sessions_on_the_same_key():
    """api-review pre-commit finding (DECISIONS #114): the skip-check in
    embed_and_insert_chunk (and the equivalent file_hash check in
    app/api/documents.py) used to be plain check-then-act with no lock —
    two callers racing on the same key could both see "not yet done" and
    both pay for a redundant external API call before either committed.
    The fix is `pg_advisory_xact_lock(hashtext(key)::bigint)` acquired
    before the check. This test proves the primitive itself actually
    blocks a second, independent session on the same key until the first
    session's transaction ends — not just that the SQL is valid syntax
    (every other test in this file already proves that by passing)."""
    import threading

    from app.db.connection import get_connection

    key = "advisory-lock-serialization-test-key"
    lock_sql = "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)"

    conn_a = get_connection()
    conn_b = get_connection()
    try:
        with conn_a.cursor() as cur:
            cur.execute(lock_sql, (key,))  # acquired; conn_a's transaction stays open

        acquired_b = threading.Event()

        def acquire_on_b() -> None:
            with conn_b.cursor() as cur:
                cur.execute(lock_sql, (key,))  # should block until conn_a commits/rolls back
            acquired_b.set()

        t = threading.Thread(target=acquire_on_b)
        t.start()
        # conn_b must NOT have acquired the lock while conn_a still holds it.
        still_blocked = not acquired_b.wait(timeout=0.5)
        assert still_blocked, "second session acquired the lock while the first still held it"

        conn_a.commit()  # releases conn_a's advisory lock
        t.join(timeout=5)
        assert acquired_b.is_set(), "second session never acquired the lock after the first released it"
    finally:
        conn_a.rollback()
        conn_b.rollback()
        conn_a.close()
        conn_b.close()


# --- Insert correctness: every Chunk field lands in the right column -------


def test_embed_and_insert_chunk_persists_all_fields(db_conn):
    chunk = _make_chunk("full-fields", text="the real text to embed")
    embedding = _valid_embedding(0.25)
    client = _fake_client(return_value=_fake_response(embedding))

    outcome = embed_and_insert_chunk(db_conn, client, chunk, commit=False)
    assert outcome == "inserted"

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT chunk_id, doc_type, chunk_text, section_label, source_url, "
            "published_date, retrieval_timestamp, start_seconds, end_seconds, "
            "embedding, embedding_model, embedded_at FROM chunks WHERE chunk_id = %s",
            (chunk.chunk_id,),
        )
        row = cur.fetchone()

    assert row is not None
    (
        chunk_id,
        doc_type,
        chunk_text,
        section_label,
        source_url,
        published_date,
        retrieval_timestamp,
        start_seconds,
        end_seconds,
        embedding_col,
        embedding_model,
        embedded_at,
    ) = row

    assert chunk_id == chunk.chunk_id
    assert doc_type == "test_doc_type"
    assert chunk_text == "the real text to embed"
    assert section_label == chunk.section_label
    assert source_url == chunk.attribution.source_url
    assert published_date == chunk.attribution.published_date
    assert retrieval_timestamp == chunk.attribution.retrieval_timestamp
    assert start_seconds is None
    assert end_seconds is None
    assert embedding_col.to_list() == pytest.approx(embedding)
    assert embedding_model == "text-embedding-3-small"
    assert embedded_at is not None


# --- Upsert semantics at the SQL layer (defensive: a concurrent re-run) ----


def test_insert_chunk_row_on_conflict_updates_text(db_conn):
    chunk = _make_chunk("upsert-test", text="original text")
    _insert_chunk_row(
        db_conn, chunk, _valid_embedding(0.1), "text-embedding-3-small", datetime.now(timezone.utc)
    )

    updated_chunk = _make_chunk("upsert-test", text="revised text")
    assert updated_chunk.chunk_id == chunk.chunk_id
    _insert_chunk_row(
        db_conn,
        updated_chunk,
        _valid_embedding(0.9),
        "text-embedding-3-small",
        datetime.now(timezone.utc),
    )

    with db_conn.cursor() as cur:
        cur.execute("SELECT chunk_text FROM chunks WHERE chunk_id = %s", (chunk.chunk_id,))
        (chunk_text,) = cur.fetchone()
    assert chunk_text == "revised text"
    db_conn.rollback()


# --- Partial-failure handling: embed succeeds, DB write fails --------------


def test_db_failure_after_successful_embed_rolls_back_and_reraises_original(db_conn, monkeypatch):
    chunk = _make_chunk("db-fail-test")
    client = _fake_client(return_value=_fake_response(_valid_embedding()))

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated DB write failure")

    monkeypatch.setattr("app.embeddings.pipeline._insert_chunk_row", _boom)

    with pytest.raises(RuntimeError, match="simulated DB write failure"):
        embed_and_insert_chunk(db_conn, client, chunk, commit=False)

    # The connection must still be usable after the pipeline's own
    # rollback() — proves this isn't left in Postgres's "current
    # transaction is aborted" state.
    with db_conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)

    assert chunk_already_embedded(db_conn, chunk.chunk_id) is False


def test_rollback_failure_does_not_mask_the_original_error(db_conn, monkeypatch):
    chunk = _make_chunk("rollback-fail-test")
    client = _fake_client(return_value=_fake_response(_valid_embedding()))

    def _boom_insert(*args, **kwargs):
        raise ValueError("original DB error")

    def _boom_rollback():
        raise RuntimeError("rollback itself also failed")

    monkeypatch.setattr("app.embeddings.pipeline._insert_chunk_row", _boom_insert)
    monkeypatch.setattr(db_conn, "rollback", _boom_rollback)

    # .claude/rules/data.md: best-effort cleanup, but always surface the
    # ORIGINAL error, not a failure encountered while cleaning up.
    with pytest.raises(ValueError, match="original DB error"):
        embed_and_insert_chunk(db_conn, client, chunk, commit=False)


# --- Batch behavior: one bad chunk doesn't abort the rest -------------------


def test_embed_and_insert_chunks_batch_continues_past_one_failure(db_conn):
    good_chunk_1 = _make_chunk("batch-good-1", text="good chunk one text")
    bad_chunk = _make_chunk("batch-bad", text="this chunk's embed call always fails")
    good_chunk_2 = _make_chunk("batch-good-2", text="good chunk two text")

    client = MagicMock()

    def _create(*, input: str, model: str, **_kwargs):
        if input == bad_chunk.text:
            raise ValueError("simulated embed failure")
        return _fake_response(_valid_embedding())

    client.embeddings.create = MagicMock(side_effect=_create)

    try:
        result = embed_and_insert_chunks(db_conn, client, [good_chunk_1, bad_chunk, good_chunk_2])

        assert result.inserted == 2
        assert result.failed == 1
        assert result.skipped == 0
        assert result.failed_chunk_ids == (bad_chunk.chunk_id,)
        assert result.total == 3

        assert chunk_already_embedded(db_conn, good_chunk_1.chunk_id) is True
        assert chunk_already_embedded(db_conn, good_chunk_2.chunk_id) is True
        assert chunk_already_embedded(db_conn, bad_chunk.chunk_id) is False
    finally:
        # Batch calls commit per-chunk (default commit=True) — clean up the
        # two real commits this test made, since this suite's rollback-at-
        # teardown convention only covers uncommitted work. In `finally` so
        # an assertion failure above still doesn't leave permanent junk
        # rows in the shared dev DB (which would skew
        # chunk_already_embedded on the next run).
        with db_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM chunks WHERE chunk_id IN (%s, %s)",
                (good_chunk_1.chunk_id, good_chunk_2.chunk_id),
            )
        db_conn.commit()
