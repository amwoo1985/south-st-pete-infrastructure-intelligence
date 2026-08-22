"""Embed-and-insert pipeline: turns a Chunk (app/chunking/base.py) into a
row in the `chunks` table (app/db/schema.py), embedding it first via
OpenAI (app/embeddings/client.py).

Idempotency (DECISIONS #58, #71): `Chunk.chunk_id` — a stable sha256 over
the chunk's own identity parts, never its text — is the pre-check key.
Before ever calling the paid embeddings API for a chunk, this module
checks whether that chunk_id already has a row in `chunks`. If so, the
chunk is skipped entirely: no API call, no write. This is what makes a
re-run over an unchanged corpus free (.claude/rules/deploy.md's
idempotency-key-and-pre-check-before-any-external-write rule).

Write ordering, and why there is no "cleanup" step here (DECISIONS #71):
this module always embeds FIRST (in memory, no side effect) and only
attempts the DB write once it already holds a complete row (chunk fields
+ embedding + embedding_model + embedded_at) to INSERT ... ON CONFLICT in
one atomic statement. There is deliberately no "insert a stub row, then
UPDATE it with the embedding" step — that would create exactly the
partial-failure state (a persisted row with a NULL embedding after a
crashed run) that .claude/rules/data.md's best-effort-cleanup rule exists
to handle, so it's avoided by construction instead of being handled well.
The one real partial-failure case that remains — the embed call succeeds
(money spent) but the subsequent DB write fails — has no corresponding
"cleanup" action on the OpenAI side: the embeddings endpoint is stateless,
it created no remote resource to delete, only a completed, unrefundable
API call. What this module does do on that failure path: roll back the
open transaction so no half-committed state lingers locally, log the
rollback outcome, and re-raise the ORIGINAL exception (not a rollback
failure) so the caller sees the real cause.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal

import openai
import psycopg

from app.chunking.base import Chunk
from app.db.schema import CHUNK_COLUMNS
from app.embeddings.client import EMBEDDING_MODEL, get_embedding

logger = logging.getLogger("embeddings.pipeline")

ChunkOutcome = Literal["inserted", "skipped", "failed"]


@dataclass(frozen=True)
class PipelineResult:
    inserted: int = 0
    skipped: int = 0
    failed: int = 0
    failed_chunk_ids: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return self.inserted + self.skipped + self.failed


def chunk_already_embedded(conn: psycopg.Connection, chunk_id: str) -> bool:
    """Pre-check: does this chunk_id already have a row? A row only ever
    gets written once its embedding is already in hand (see module
    docstring), so existence alone — no need to also check embedding IS
    NOT NULL — is a complete "already done" signal."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM chunks WHERE chunk_id = %s", (chunk_id,))
        return cur.fetchone() is not None


_INSERT_SQL = f"""
INSERT INTO chunks ({", ".join(CHUNK_COLUMNS)})
VALUES ({", ".join(["%s"] * len(CHUNK_COLUMNS))})
ON CONFLICT (chunk_id) DO UPDATE SET
    doc_type = EXCLUDED.doc_type,
    chunk_text = EXCLUDED.chunk_text,
    section_label = EXCLUDED.section_label,
    source_url = EXCLUDED.source_url,
    published_date = EXCLUDED.published_date,
    retrieval_timestamp = EXCLUDED.retrieval_timestamp,
    start_seconds = EXCLUDED.start_seconds,
    end_seconds = EXCLUDED.end_seconds,
    embedding = EXCLUDED.embedding,
    embedding_model = EXCLUDED.embedding_model,
    embedded_at = EXCLUDED.embedded_at
"""


def _insert_chunk_row(
    conn: psycopg.Connection,
    chunk: Chunk,
    embedding: list[float],
    embedding_model: str,
    embedded_at: datetime,
) -> None:
    values = (
        chunk.chunk_id,
        chunk.doc_type,
        chunk.text,
        chunk.section_label,
        chunk.attribution.source_url,
        chunk.attribution.published_date,
        chunk.attribution.retrieval_timestamp,
        chunk.start_seconds,
        chunk.end_seconds,
        embedding,
        embedding_model,
        embedded_at,
    )
    with conn.cursor() as cur:
        cur.execute(_INSERT_SQL, values)


def embed_and_insert_chunk(
    conn: psycopg.Connection,
    client: openai.OpenAI,
    chunk: Chunk,
    *,
    commit: bool = True,
) -> ChunkOutcome:
    """Embeds and upserts one Chunk. Returns "skipped" without any API
    call if chunk_id already has a row. Returns "inserted" on success.
    Re-raises the original exception on failure (embed-call or DB-write) —
    the caller (embed_and_insert_chunks, or a script) decides whether that
    should stop a batch or just be counted as one failure.

    `commit`: pass False when the caller manages its own transaction (e.g.
    a test that wants to roll back everything at the end) — see
    tests/db/test_pipeline.py.
    """
    if chunk_already_embedded(conn, chunk.chunk_id):
        return "skipped"

    # Embed first — pure API call, no DB side effect yet, nothing to
    # clean up if this raises.
    embedding = get_embedding(client, chunk.text)

    try:
        _insert_chunk_row(
            conn,
            chunk,
            embedding,
            EMBEDDING_MODEL,
            datetime.now(timezone.utc),
        )
        if commit:
            conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except Exception as rollback_exc:
            # Log, but never let a rollback failure mask the real cause —
            # .claude/rules/data.md: "return the original error, not the
            # cleanup error."
            logger.error(
                "rollback failed after insert failure for chunk_id=%s: %s",
                chunk.chunk_id,
                rollback_exc,
            )
        logger.error(
            "DB insert failed for chunk_id=%s after a successful (already-"
            "billed) embed call: %s",
            chunk.chunk_id,
            exc,
        )
        raise

    return "inserted"


def embed_and_insert_chunks(
    conn: psycopg.Connection,
    client: openai.OpenAI,
    chunks: Iterable[Chunk],
) -> PipelineResult:
    """Batch version: processes every chunk, continuing past individual
    failures (logged, counted) rather than letting one bad chunk abort an
    entire corpus run — but never silently drops a failure, per
    .claude/rules/crawler.md's fail-loud spirit applied at the item level:
    a non-zero `failed` count in the returned PipelineResult, plus each
    failed chunk_id, makes failures visible rather than looking like
    "nothing new happened."

    Exception: a DB-connection-level failure (psycopg.OperationalError /
    InterfaceError — e.g. a dropped connection mid-run) is NOT counted as
    a per-chunk failure. It propagates and aborts the batch immediately,
    because it isn't information about that one chunk — every remaining
    chunk would fail identically, and replaying a systemic failure as N
    misleading "failed chunk" entries would itself violate the fail-loud
    rule it's meant to satisfy.
    """
    inserted = skipped = failed = 0
    failed_ids: list[str] = []

    for chunk in chunks:
        try:
            outcome = embed_and_insert_chunk(conn, client, chunk)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            # Connection-level failure (dropped connection, server gone
            # away, etc) — NOT a per-chunk problem. Every remaining chunk
            # in this batch would fail the same way for the same reason,
            # so counting each as an independent "failed chunk" would
            # misrepresent "the whole run broke" as "N chunks each
            # happened to be bad" (.claude/rules/crawler.md's fail-loud
            # spirit: a systemic failure must not look like ordinary,
            # expected per-item noise). Abort the batch loudly instead.
            logger.error(
                "aborting batch: DB connection failure while processing "
                "chunk_id=%s after %d inserted, %d skipped, %d failed so far",
                chunk.chunk_id,
                inserted,
                skipped,
                failed,
            )
            raise
        except Exception:
            failed += 1
            failed_ids.append(chunk.chunk_id)
            continue

        if outcome == "inserted":
            inserted += 1
        elif outcome == "skipped":
            skipped += 1

    return PipelineResult(
        inserted=inserted,
        skipped=skipped,
        failed=failed,
        failed_chunk_ids=tuple(failed_ids),
    )
