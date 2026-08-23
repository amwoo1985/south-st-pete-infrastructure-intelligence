"""``document_uploads`` table DDL — the idempotency ledger for
POST /documents/upload (app/api/documents.py). Mirrors
app/granicus/schema.py's pattern: a sibling apply_*_schema() function
rather than folding into app/db/schema.py's apply_schema(), which
docstring-scopes itself to the ``chunks`` table only (DECISIONS #71).
Same reasoning applies here — a second, unrelated table's DDL doesn't
belong bolted onto a module that already declares a narrower scope for
itself; app/api/main.py calls both apply functions at startup.

One row per uploaded file's content hash (file_hash = sha256 of the raw
uploaded bytes), never per upload attempt — this is what makes the
upload endpoint's idempotency-on-hash contract a DB-enforced fact
(PRIMARY KEY), not just an application-level convention.
"""

from __future__ import annotations

import psycopg

# Explicit column list, reused by every INSERT/SELECT/UPDATE this module
# and app/api/documents.py write — .claude/rules/data.md bars `SELECT *`.
#
# Column <-> concept mapping:
#   file_hash            <- sha256 hex digest of the raw uploaded bytes;
#                           the natural idempotency key (same bytes ->
#                           same hash -> same row, regardless of upload
#                           attempt count or filename).
#   original_filename     <- the client-supplied filename, for a human-
#                           readable /documents/upload response; not
#                           part of the identity key (a re-upload of the
#                           same bytes under a different filename is
#                           still the same document).
#   content_type           <- canonical mime string this app assigned
#                           after validating kind (pdf/docx/txt) —
#                           app/api/documents.py's own determination, not
#                           blindly trusted from the client's
#                           Content-Type header.
#   status                  <- 'processing' | 'completed' | 'failed'
#   failure_reason           <- NULL unless status='failed'; never left
#                           silently stuck (.claude/rules/crawler.md
#                           fail-loud, applied to this pipeline).
#   chunk_count               <- NULL until status='completed'
#   uploaded_at                <- when this file_hash was FIRST recorded
#                           (not touched on a retry's upsert — see
#                           app/api/documents.py's _UPSERT_PROCESSING_SQL)
#   completed_at                <- NULL until status='completed'
DOCUMENT_UPLOAD_COLUMNS: tuple[str, ...] = (
    "file_hash",
    "original_filename",
    "content_type",
    "status",
    "failure_reason",
    "chunk_count",
    "uploaded_at",
    "completed_at",
)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS document_uploads (
    file_hash TEXT PRIMARY KEY,
    original_filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('processing', 'completed', 'failed')),
    failure_reason TEXT,
    chunk_count INTEGER,
    uploaded_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ
);
"""


def apply_document_uploads_schema(conn: psycopg.Connection) -> None:
    """Idempotent: safe to call on every process start (CREATE TABLE IF
    NOT EXISTS). Commits its own transaction — call this once at app
    startup (app/api/main.py's lifespan), not per-request."""
    with conn.cursor() as cur:
        cur.execute(_CREATE_TABLE_SQL)
    conn.commit()
