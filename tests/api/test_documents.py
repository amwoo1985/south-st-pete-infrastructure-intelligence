"""Tests for POST /documents/upload's hash-based idempotency pre-check
(app/api/documents.py) — the four branches: completed (short-circuit, no
reprocessing), failed (retry-eligible), processing (retry-eligible), and
no existing row (fresh upload). The actual parse/chunk/embed work
(`_process_upload_background`) is mocked out for every test here — these
tests are about the pre-check/upsert decision and response shape, not
the chunker or the embeddings pipeline (covered separately by
tests/chunking/test_uploaded_document.py and the existing embeddings
pipeline tests). This also means no real OpenAI call happens even though
FastAPI's TestClient runs BackgroundTasks synchronously within the
request cycle (DECISIONS #72/#90's mocking discipline).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.db.connection import get_connection


def _cleanup(file_hash: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM document_uploads WHERE file_hash = %s", (file_hash,))
        conn.commit()
    finally:
        conn.close()


def _insert_upload_row(file_hash: str, status: str, **overrides) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO document_uploads
                    (file_hash, original_filename, content_type, status,
                     failure_reason, chunk_count, uploaded_at, completed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (file_hash) DO NOTHING
                """,
                (
                    file_hash,
                    overrides.get("original_filename", "preexisting.txt"),
                    overrides.get("content_type", "text/plain"),
                    status,
                    overrides.get("failure_reason"),
                    overrides.get("chunk_count"),
                    overrides.get("uploaded_at", datetime.now(timezone.utc)),
                    overrides.get("completed_at"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def mock_background(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr("app.api.documents._process_upload_background", mock)
    return mock


def test_new_upload_returns_202_and_schedules_processing(client, mock_background):
    content = b"Hello, this is a brand-new upload with no prior history."
    file_hash = hashlib.sha256(content).hexdigest()
    try:
        resp = client.post(
            "/documents/upload",
            files={"file": ("new_upload.txt", content, "text/plain")},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["file_hash"] == file_hash
        assert body["status"] == "processing"
        assert body["chunk_count"] is None
        assert body["completed_at"] is None

        mock_background.assert_called_once()
        called_hash, called_bytes, called_filename, called_kind = mock_background.call_args[0]
        assert called_hash == file_hash
        assert called_bytes == content
        assert called_filename == "new_upload.txt"
        assert called_kind == "txt"
    finally:
        _cleanup(file_hash)


def test_completed_upload_short_circuits_with_200_and_no_reprocessing(client, mock_background):
    content = b"This document was already fully processed before."
    file_hash = hashlib.sha256(content).hexdigest()
    _insert_upload_row(
        file_hash,
        status="completed",
        original_filename="already_done.txt",
        chunk_count=3,
        completed_at=datetime.now(timezone.utc),
    )
    try:
        resp = client.post(
            "/documents/upload",
            files={"file": ("already_done.txt", content, "text/plain")},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["file_hash"] == file_hash
        assert body["status"] == "completed"
        assert body["chunk_count"] == 3
        assert body["completed_at"] is not None

        # The actual idempotency-on-hash contract: no reprocessing, no
        # new embed calls scheduled at all.
        mock_background.assert_not_called()
    finally:
        _cleanup(file_hash)


def test_failed_upload_is_retried_and_failure_reason_cleared(client, mock_background):
    content = b"This document failed to process on a previous attempt."
    file_hash = hashlib.sha256(content).hexdigest()
    _insert_upload_row(
        file_hash,
        status="failed",
        original_filename="broke_last_time.txt",
        failure_reason="RuntimeError: something broke",
    )
    try:
        resp = client.post(
            "/documents/upload",
            files={"file": ("broke_last_time.txt", content, "text/plain")},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "processing"

        mock_background.assert_called_once()

        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, failure_reason FROM document_uploads WHERE file_hash = %s",
                    (file_hash,),
                )
                status, failure_reason = cur.fetchone()
        finally:
            conn.close()
        assert status == "processing"
        assert failure_reason is None  # cleared on retry, never left stale
    finally:
        _cleanup(file_hash)


def test_stuck_processing_upload_is_treated_as_retry_eligible(client, mock_background):
    # Self-healing case: a background task that died mid-flight (process
    # restart, OOM) leaves a row stuck at 'processing' forever with no
    # separate stale-row-recovery sweep for this one-shot job shape (see
    # app/api/documents.py's module docstring) — the next identical-hash
    # upload attempt is what recovers it.
    content = b"This document's background task apparently died mid-flight."
    file_hash = hashlib.sha256(content).hexdigest()
    _insert_upload_row(file_hash, status="processing", original_filename="stuck.txt")
    try:
        resp = client.post(
            "/documents/upload",
            files={"file": ("stuck.txt", content, "text/plain")},
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "processing"
        mock_background.assert_called_once()
    finally:
        _cleanup(file_hash)


def test_unsupported_file_type_rejected_with_400(client, mock_background):
    resp = client.post(
        "/documents/upload",
        files={"file": ("malware.exe", b"binary content", "application/octet-stream")},
    )
    assert resp.status_code == 400
    mock_background.assert_not_called()


def test_empty_file_rejected_with_400(client, mock_background):
    resp = client.post(
        "/documents/upload",
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert resp.status_code == 400
    mock_background.assert_not_called()


def test_oversized_file_rejected_with_400(client, mock_background, monkeypatch):
    # Monkeypatch the cap down rather than actually allocating/uploading
    # a real 20MB+ payload in a test.
    monkeypatch.setattr("app.api.documents.MAX_UPLOAD_BYTES", 10)
    resp = client.post(
        "/documents/upload",
        files={"file": ("too_big.txt", b"x" * 11, "text/plain")},
    )
    assert resp.status_code == 400
    mock_background.assert_not_called()


def test_docx_extension_recognized_by_extension_not_just_content_type(client, mock_background):
    # A minimal but real DOCX byte sequence, built the same way
    # tests/chunking/test_uploaded_document.py does, sent with a generic
    # Content-Type — extension-based detection should still work.
    from io import BytesIO

    from docx import Document as DocxDocument

    document = DocxDocument()
    document.add_paragraph("Real DOCX content for the extension-detection test.")
    buf = BytesIO()
    document.save(buf)
    content = buf.getvalue()
    file_hash = hashlib.sha256(content).hexdigest()
    try:
        resp = client.post(
            "/documents/upload",
            files={"file": ("real.docx", content, "application/octet-stream")},
        )
        assert resp.status_code == 202
        mock_background.assert_called_once()
        called_kind = mock_background.call_args[0][3]
        assert called_kind == "docx"
    finally:
        _cleanup(file_hash)
