"""Tests for GET /sources/{doc_id} (app/api/sources.py) — doc_id IS
chunk_id. Covers the 404 case directly; the found case is exercised
end-to-end by tests/api/test_query.py's citation-lookup test (a real
inserted chunk row), not duplicated here.
"""

from __future__ import annotations

import uuid


def test_source_404_when_chunk_id_not_found(client):
    missing_id = f"does-not-exist-{uuid.uuid4()}"
    resp = client.get(f"/sources/{missing_id}")
    assert resp.status_code == 404
    body = resp.json()
    assert "detail" in body
    assert missing_id in body["detail"]  # echoing the client's own input back is fine


def test_source_found_returns_full_record_minus_embedding(client):
    from datetime import date, datetime, timezone

    from app.db.connection import get_connection
    from app.db.schema import CHUNK_COLUMNS

    chunk_id = "sourcetest" + "0" * 22
    values = {
        "chunk_id": chunk_id,
        "doc_type": "uploaded_document",
        "chunk_text": "Some real chunk text for the sources endpoint test.",
        "section_label": "Section B",
        "source_url": "upload://sourcehash",
        "published_date": date(2026, 2, 2),
        "retrieval_timestamp": datetime.now(timezone.utc),
        "start_seconds": None,
        "end_seconds": None,
        "embedding": None,
        "embedding_model": None,
        "embedded_at": None,
    }
    cols = ", ".join(CHUNK_COLUMNS)
    placeholders = ", ".join(["%s"] * len(CHUNK_COLUMNS))
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO chunks ({cols}) VALUES ({placeholders}) "
                "ON CONFLICT (chunk_id) DO NOTHING",
                tuple(values[c] for c in CHUNK_COLUMNS),
            )
        conn.commit()

        resp = client.get(f"/sources/{chunk_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["chunk_id"] == chunk_id
        assert body["doc_type"] == "uploaded_document"
        assert body["chunk_text"] == values["chunk_text"]
        assert body["section_label"] == "Section B"
        assert body["source_url"] == "upload://sourcehash"
        assert body["published_date"] == "2026-02-02"
        assert "embedding" not in body  # never ship the 1536-float vector
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chunks WHERE chunk_id = %s", (chunk_id,))
        conn.commit()
        conn.close()
