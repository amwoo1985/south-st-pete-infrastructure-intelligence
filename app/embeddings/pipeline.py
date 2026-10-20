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

Content-staleness (DECISIONS #154): chunk_id is deliberately
identity-derived, never text-derived (app/chunking/base.py's
make_chunk_id docstring) — so a source page whose body text is edited
without renaming its section/row/item identity produces the SAME
chunk_id as before. A bare "chunk_id already has a row -> skip" pre-check
(the ONLY check this module used before #154) can never tell that case
apart from "genuinely unchanged" — the old, now-stale chunk_text and
embedding would stay in `chunks` forever, silently never refreshed. This
was a real, disclosed gap (scripts/run_live_recrawl.py's own module
docstring, closed by this round): `content_hash` (app/db/schema.py) — a
sha256 of the chunk's own CURRENT text, computed at insert/update time —
is the second, content-derived signal that closes it. The pre-check is
now two-step, not one-step:
  1. chunk_id has no row at all -> INSERT (new content).
  2. chunk_id has a row AND its stored content_hash matches this chunk's
     freshly-computed hash -> SKIP (truly unchanged; no embed call —
     this is the property that makes a weekly recrawl cheap, DECISIONS
     #126/#134, and it is unchanged by this round: comparing a hash is a
     local, free operation, done before the paid embed call either way).
  3. chunk_id has a row AND its stored content_hash differs -> RE-EMBED
     (a real, paid API call — but only because the text genuinely
     changed) and UPDATE the existing row in place via the same
     `INSERT ... ON CONFLICT DO UPDATE` used for a brand-new row (same
     chunk_id, new chunk_text/embedding/content_hash/embedded_at) — never
     a second, duplicate row.

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

import hashlib
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

ChunkOutcome = Literal["inserted", "updated", "skipped", "failed"]


@dataclass(frozen=True)
class PipelineResult:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    failed_chunk_ids: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.skipped + self.failed


def compute_content_hash(text: str) -> str:
    """Sha256 hex digest of a chunk's own CURRENT text — the content-
    staleness detection key (DECISIONS #154, see module docstring).
    Deliberately the full 64-char digest, not make_chunk_id's 32-char
    truncation: this isn't a primary key, so the extra collision-
    resistance headroom costs nothing to keep, and there's no reason to
    match a different column's truncation convention."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_already_embedded(conn: psycopg.Connection, chunk_id: str) -> bool:
    """Pure existence probe: does this chunk_id have a row at all? As of
    DECISIONS #154 this has no production caller (tests/db/test_pipeline.py
    is the only remaining user) — kept as a small, honestly-named public
    utility rather than deleted, since "has this identity ever been seen"
    is still a real, distinct question from "is this identity's content
    still current" that a future caller could legitimately want without
    paying for the extra content_hash lookup. NOT used as embed_and_insert_
    chunk's own pre-check since DECISIONS #154 — that now needs the
    stored content_hash too (see _existing_content_hash below), not just
    existence, to tell "truly unchanged" apart from "same identity,
    changed text.\""""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM chunks WHERE chunk_id = %s", (chunk_id,))
        return cur.fetchone() is not None


def _existing_content_hash(conn: psycopg.Connection, chunk_id: str) -> tuple[bool, str | None]:
    """Pre-check (DECISIONS #154): returns (row_exists, stored_content_hash).
    `stored_content_hash` can itself be None even when `row_exists` is True
    — a real, transient mid-migration state (a row written before the
    content_hash backfill/lock in app/db/schema.py has run against it).
    That NULL is deliberately treated by the caller the same as "differs
    from the new hash" (re-embed and fill it in for real), not as a
    special case — it converges to a correct, populated content_hash the
    next time this chunk_id is processed either way."""
    with conn.cursor() as cur:
        cur.execute("SELECT content_hash FROM chunks WHERE chunk_id = %s", (chunk_id,))
        row = cur.fetchone()
    if row is None:
        return False, None
    return True, row[0]


_INSERT_SQL = f"""
INSERT INTO chunks ({", ".join(CHUNK_COLUMNS)})
VALUES ({", ".join(["%s"] * len(CHUNK_COLUMNS))})
ON CONFLICT (chunk_id) DO UPDATE SET
    doc_type = EXCLUDED.doc_type,
    chunk_text = EXCLUDED.chunk_text,
    content_hash = EXCLUDED.content_hash,
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
    content_hash: str,
) -> None:
    values = (
        chunk.chunk_id,
        chunk.doc_type,
        chunk.text,
        content_hash,
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
    """Embeds and upserts one Chunk. Returns "skipped" without any API call
    if chunk_id already has a row AND its stored content_hash matches this
    chunk's current text (DECISIONS #154). Returns "inserted" on success
    for a brand-new chunk_id, or "updated" on success for an existing
    chunk_id whose text genuinely changed (same row, real re-embed, upsert
    in place — never a duplicate). Re-raises the original exception on
    failure (embed-call or DB-write) — the caller (embed_and_insert_chunks,
    or a script) decides whether that should stop a batch or just be
    counted as one failure.

    `commit`: pass False when the caller manages its own transaction (e.g.
    a test that wants to roll back everything at the end) — see
    tests/db/test_pipeline.py.

    Concurrency (api-review pre-commit finding, DECISIONS #114): the
    skip-check above is plain check-then-act with no lock, so two callers
    racing on the same chunk_id (e.g. two concurrent uploads whose content
    happens to produce the same chunk) could both see "not yet embedded"
    and both pay for the same embedding API call before either commits.
    `pg_advisory_xact_lock` closes that window: it blocks a second caller
    on the same chunk_id until the first's transaction commits (or rolls
    back) — transaction-scoped, so it needs no explicit unlock, and it
    fits this module's existing commit-per-chunk boundary exactly. Once
    unblocked, the second caller's own `_existing_content_hash` check
    correctly sees the row (and content_hash) the first caller just
    committed, and skips.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)", (chunk.chunk_id,))

    new_content_hash = compute_content_hash(chunk.text)
    row_exists, stored_content_hash = _existing_content_hash(conn, chunk.chunk_id)

    if row_exists and stored_content_hash == new_content_hash:
        if commit:
            # Release the advisory lock now rather than holding it open
            # until whatever this connection's next unrelated commit
            # happens to be — a plain "skip" outcome has nothing else to
            # commit, so there's no reason to keep another chunk_id's
            # lock request waiting behind this one.
            conn.commit()
        return "skipped"

    # Either a brand-new chunk_id, or an existing one whose stored
    # content_hash differs (or is still NULL, mid-migration — see
    # _existing_content_hash) from the chunk's current text (DECISIONS
    # #154). Either way there is real, changed-or-new content to embed —
    # pure API call, no DB side effect yet, nothing to clean up if this
    # raises.
    embedding = get_embedding(client, chunk.text)

    try:
        _insert_chunk_row(
            conn,
            chunk,
            embedding,
            EMBEDDING_MODEL,
            datetime.now(timezone.utc),
            new_content_hash,
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

    return "updated" if row_exists else "inserted"


def embed_and_insert_chunks(
    conn: psycopg.Connection,
    client: openai.OpenAI,
    chunks: Iterable[Chunk],
) -> PipelineResult:
    """Batch version: processes every chunk, continuing past individual
    failures (logged, counted) rather than letting one bad chunk abort an
    entire corpus run — but never silently drops a failure, per
    .claude/rules/data.md's fail-loud spirit applied at the item level:
    a non-zero `failed` count in the returned PipelineResult, plus each
    failed chunk_id, makes failures visible rather than looking like
    "nothing new happened."

    `PipelineResult.updated` (DECISIONS #154) counts chunks whose identity
    (chunk_id) already existed but whose content genuinely changed and was
    re-embedded/upserted in place — distinct from `inserted` (brand-new
    chunk_id) and `skipped` (unchanged); `total` sums all four outcomes.

    Exception: a DB-connection-level failure (psycopg.OperationalError /
    InterfaceError — e.g. a dropped connection mid-run) is NOT counted as
    a per-chunk failure. It propagates and aborts the batch immediately,
    because it isn't information about that one chunk — every remaining
    chunk would fail identically, and replaying a systemic failure as N
    misleading "failed chunk" entries would itself violate the fail-loud
    rule it's meant to satisfy.
    """
    inserted = updated = skipped = failed = 0
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
            # happened to be bad" (.claude/rules/data.md's fail-loud
            # spirit: a systemic failure must not look like ordinary,
            # expected per-item noise). Abort the batch loudly instead.
            logger.error(
                "aborting batch: DB connection failure while processing "
                "chunk_id=%s after %d inserted, %d updated, %d skipped, "
                "%d failed so far",
                chunk.chunk_id,
                inserted,
                updated,
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
        elif outcome == "updated":
            updated += 1
        elif outcome == "skipped":
            skipped += 1

    return PipelineResult(
        inserted=inserted,
        updated=updated,
        skipped=skipped,
        failed=failed,
        failed_chunk_ids=tuple(failed_ids),
    )
