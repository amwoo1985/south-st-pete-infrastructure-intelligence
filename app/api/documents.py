"""POST /documents/upload — accepts a PDF/DOCX/TXT file, hashes it,
parses/chunks/embeds it as a FastAPI BackgroundTask, and stores an
idempotency ledger row in `document_uploads` (app/api/schema.py).

BackgroundTasks, not a Granicus-style polling worker table (directive,
already decided): this is a one-shot job (seconds, not hours) over a
file already fully in memory, not a multi-hour recurring transcription
job — .claude/rules/crawler.md's worker-table guidance
("FOR UPDATE SKIP LOCKED claiming, stale-row recovery") is written for
that different shape of problem. `document_uploads.status` still gives
this a self-healing retry story (see below), just without a claim table.

Idempotency contract (directive, already decided):
  - `status='completed'` for this file_hash -> return the existing
    record immediately, HTTP 200. No reprocessing, no new embedding API
    calls — the actual idempotency-on-hash contract, not just
    "don't crash on a duplicate" (.claude/rules/deploy.md's
    idempotency-key-and-pre-check rule).
  - `status='processing'` or `'failed'`, or no row at all -> upsert to
    `status='processing'` (clearing failure_reason), schedule the
    background job, return HTTP 202. Treating 'processing' as retry-
    eligible (not "already in flight, do nothing") is deliberate: a
    background task can die (process restart, OOM) without ever writing
    a terminal status, and there is no separate stale-row-recovery
    sweep for this one-shot job shape — the NEXT identical-hash upload
    attempt is what self-heals a stuck 'processing' row, exactly the way
    a 'failed' row already self-heals on retry.

Chunk ids are derived deterministically from file_hash + position
(app/chunking/uploaded_document.py) — so a retry reprocessing the same
bytes produces the SAME chunk_ids and upserts cleanly via
app.embeddings.pipeline.embed_and_insert_chunk's ON CONFLICT logic,
rather than duplicating rows or re-paying for chunks already embedded.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Response, UploadFile

from app.api.dependencies import get_db_conn, get_openai_client
from app.api.schemas import DocumentUploadResponse
from app.chunking.uploaded_document import chunk_uploaded_document
from app.db.connection import get_connection
from app.embeddings.pipeline import embed_and_insert_chunks

logger = logging.getLogger("api.documents")

router = APIRouter()

# Read+hash+parse all happens in memory (see module docstring — this is a
# one-shot job over a fully-buffered file, not a stream). 20MB is well
# above any realistic CBA/grant/meeting-record document (this corpus's
# real PDFs are agenda packets and grant guidelines, not multi-hundred-
# page scans) while still bounding worst-case memory/parse time for a
# single request.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# Known gap (self-review, this round, not fixed here — flagged to the
# orchestrator/deploy-infra): this check runs AFTER `await file.read()`
# has already materialized the full upload as one in-memory bytes
# object, so it stops oversized files from being parsed/chunked/embedded,
# but does NOT stop a client from forcing this process to buffer an
# arbitrarily large request body first. A hard body-size limit enforced
# one layer up — at the reverse proxy / ALB, before a request reaches
# this app at all — is the real fix for that; this app-level check is
# real defense-in-depth for the "how far does the request get" question,
# not a substitute for that.

_EXTENSION_TO_KIND = {".pdf": "pdf", ".docx": "docx", ".txt": "txt"}
_CONTENT_TYPE_TO_KIND = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/plain": "txt",
}
# The canonical content_type this app stores/trusts for each kind, once
# determined — never the client-supplied header as-is (a client can send
# an absent or generic "application/octet-stream" Content-Type; this app
# decides its own canonical value once `kind` is known).
_KIND_TO_CANONICAL_CONTENT_TYPE = {v: k for k, v in _CONTENT_TYPE_TO_KIND.items()}

_SELECT_UPLOAD_SQL = (
    "SELECT status, original_filename, chunk_count, uploaded_at, completed_at "
    "FROM document_uploads WHERE file_hash = %s"
)

# uploaded_at is deliberately NOT in the SET clause: on a retry (conflict)
# it stays at the row's original value (see module docstring — a retry is
# still "the same upload," first-seen time shouldn't reset). chunk_count/
# completed_at ARE reset to NULL here as a defensive measure — this
# branch only ever runs for 'processing'/'failed'/no-row cases (a
# 'completed' row short-circuits before reaching this statement), so
# both should already be NULL/stale, but resetting them explicitly keeps
# this statement correct even if that invariant is ever violated.
_UPSERT_PROCESSING_SQL = """
INSERT INTO document_uploads
    (file_hash, original_filename, content_type, status, failure_reason,
     chunk_count, uploaded_at, completed_at)
VALUES (%s, %s, %s, 'processing', NULL, NULL, %s, NULL)
ON CONFLICT (file_hash) DO UPDATE SET
    original_filename = EXCLUDED.original_filename,
    content_type = EXCLUDED.content_type,
    status = 'processing',
    failure_reason = NULL,
    chunk_count = NULL,
    completed_at = NULL
RETURNING uploaded_at
"""

_MARK_COMPLETED_SQL = (
    "UPDATE document_uploads SET status = 'completed', chunk_count = %s, "
    "completed_at = %s WHERE file_hash = %s"
)
_MARK_FAILED_SQL = (
    "UPDATE document_uploads SET status = 'failed', failure_reason = %s WHERE file_hash = %s"
)


def _determine_kind(filename: str | None, content_type: str | None) -> str | None:
    """Extension is checked first (more reliable in practice — many
    upload clients, including curl and some browsers, send a generic or
    absent Content-Type), falling back to the client-supplied
    Content-Type header only if the filename has no recognized
    extension. Returns None (caller raises 400) if neither matches."""
    if filename:
        ext = Path(filename).suffix.lower()
        if ext in _EXTENSION_TO_KIND:
            return _EXTENSION_TO_KIND[ext]
    if content_type:
        kind = _CONTENT_TYPE_TO_KIND.get(content_type.lower())
        if kind:
            return kind
    return None


@router.post("/documents/upload", response_model=DocumentUploadResponse, status_code=202)
async def upload_document(
    background_tasks: BackgroundTasks,
    response: Response,
    file: UploadFile = File(...),
    conn: psycopg.Connection = Depends(get_db_conn),
) -> DocumentUploadResponse:
    raw_bytes = await file.read()

    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)}MB upload limit.",
        )

    kind = _determine_kind(file.filename, file.content_type)
    if kind is None:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type — only PDF, DOCX, and TXT files are accepted.",
        )

    file_hash = hashlib.sha256(raw_bytes).hexdigest()
    filename = file.filename or f"upload.{kind}"
    content_type = _KIND_TO_CANONICAL_CONTENT_TYPE[kind]

    with conn.cursor() as cur:
        cur.execute(_SELECT_UPLOAD_SQL, (file_hash,))
        existing = cur.fetchone()

    if existing is not None:
        status, original_filename, chunk_count, uploaded_at, completed_at = existing
        if status == "completed":
            logger.info(
                "upload file_hash=%s: already completed (%d chunks) — short-circuit, "
                "no reprocessing",
                file_hash,
                chunk_count,
            )
            response.status_code = 200
            return DocumentUploadResponse(
                file_hash=file_hash,
                status="completed",
                original_filename=original_filename,
                chunk_count=chunk_count,
                uploaded_at=uploaded_at,
                completed_at=completed_at,
            )
        # status in ('processing', 'failed') -> retry-eligible, falls
        # through to the upsert-and-reprocess path below exactly like a
        # brand-new file_hash (see module docstring).

    with conn.cursor() as cur:
        cur.execute(
            _UPSERT_PROCESSING_SQL,
            (file_hash, filename, content_type, datetime.now(timezone.utc)),
        )
        (uploaded_at,) = cur.fetchone()
    conn.commit()

    background_tasks.add_task(_process_upload_background, file_hash, raw_bytes, filename, kind)

    response.status_code = 202
    return DocumentUploadResponse(
        file_hash=file_hash,
        status="processing",
        original_filename=filename,
        chunk_count=None,
        uploaded_at=uploaded_at,
        completed_at=None,
    )


def _process_upload_background(file_hash: str, raw_bytes: bytes, filename: str, kind: str) -> None:
    """Runs after the HTTP response is already sent (FastAPI
    BackgroundTasks). Owns its OWN DB connection — never reuses the
    request-scoped one from upload_document(), since that connection is
    closed by get_db_conn()'s generator teardown once the request cycle
    ends, and this task's lifetime is not guaranteed to nest inside that
    teardown. The OpenAI client IS safely reused (see
    app/api/dependencies.py's module-level singleton reasoning).

    Best-effort cleanup on failure (.claude/rules/data.md): rolls back the
    open transaction, then makes its own separate best-effort attempt to
    persist status='failed' with a real failure_reason — this is what
    makes a stuck row self-heal on the next identical-hash retry (see
    module docstring). A failure in that second attempt is logged, never
    allowed to raise past this function (it already runs off the request
    thread; an uncaught exception here would only be silently swallowed
    by Starlette's background-task runner, not surfaced anywhere useful)."""
    conn = get_connection()
    client = get_openai_client()
    try:
        chunks = chunk_uploaded_document(
            raw_bytes, filename=filename, file_hash=file_hash, kind=kind
        )
        result = embed_and_insert_chunks(conn, client, chunks)
        if result.failed:
            # Fail loud rather than silently marking 'completed' with a
            # partial corpus (.claude/rules/crawler.md) — e.g. a single
            # pathological paragraph over the embeddings API's 24,000-
            # char ceiling (app/embeddings/client.py's
            # EmbeddingInputTooLargeError) would land here. The
            # successfully-inserted chunks from this pass are NOT rolled
            # back (embed_and_insert_chunk already committed each one
            # individually) — a retry will skip them (idempotent,
            # deterministic chunk_ids) and only re-attempt the failures.
            raise RuntimeError(
                f"{result.failed}/{result.total} chunk(s) failed to embed/insert "
                f"(failed_chunk_ids={result.failed_chunk_ids})"
            )

        with conn.cursor() as cur:
            cur.execute(_MARK_COMPLETED_SQL, (len(chunks), datetime.now(timezone.utc), file_hash))
        conn.commit()
        logger.info("upload file_hash=%s: completed, %d chunk(s)", file_hash, len(chunks))
    except Exception as exc:
        try:
            conn.rollback()
        except Exception as rollback_exc:
            logger.error(
                "upload file_hash=%s: rollback failed after processing failure: %s",
                file_hash,
                rollback_exc,
            )
        failure_reason = f"{type(exc).__name__}: {exc}"
        logger.error("upload file_hash=%s: processing failed: %s", file_hash, failure_reason)
        try:
            with conn.cursor() as cur:
                cur.execute(_MARK_FAILED_SQL, (failure_reason, file_hash))
            conn.commit()
        except Exception as mark_failed_exc:
            logger.error(
                "upload file_hash=%s: failed to persist failure status (row may be "
                "stuck at 'processing' until the next identical-hash retry re-upserts "
                "it): %s",
                file_hash,
                mark_failed_exc,
            )
    finally:
        conn.close()
