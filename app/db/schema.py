"""Phase E schema: one ``chunks`` table holding both a chunk's content/
attribution and its embedding vector. See DECISIONS #71 for the full
one-table-vs-two-tables reasoning; short version: a Chunk and its
embedding are always 1:1 in this pipeline (no multi-vector-per-chunk
strategy exists or is planned), so a join buys nothing and a two-table
design would double the write surface (and therefore the partial-failure
surface .claude/rules/data.md's cleanup rule is about) for no benefit.

This module owns the ``chunks`` column list. The DDL itself lives in the
ordered migrations under app/db/migrations/ (DECISIONS #160).
"""

from __future__ import annotations

# Explicit column list, reused by every INSERT/SELECT this pipeline writes
# — .claude/rules/data.md bars `SELECT *` and wants one named column-list
# constant per table, not ad hoc column lists scattered across queries.
#
# Column <-> Chunk field mapping (app/chunking/base.py):
#   chunk_id             <- Chunk.chunk_id
#   doc_type             <- Chunk.doc_type
#   chunk_text           <- Chunk.text (renamed from the dataclass's `text`
#                           to avoid a column literally named `text` reading
#                           ambiguously next to Postgres's `text` type)
#   content_hash          <- computed by app.embeddings.pipeline from
#                           Chunk.text (sha256 hex digest) — DECISIONS #154.
#                           NOT a Chunk field: chunk_id (above) is derived
#                           from IDENTITY only and deliberately never
#                           changes when a source's body text is edited
#                           (see app/chunking/base.py's make_chunk_id
#                           docstring) — content_hash is the separate,
#                           content-derived signal that lets the embed
#                           pipeline tell "truly unchanged, skip" apart
#                           from "same identity, different text, re-embed"
#                           on a recrawl. See _ADD_CONTENT_HASH_COLUMN_SQL/
#                           _LOCK_CONTENT_HASH_NOT_NULL_SQL below for the
#                           nullable-then-locked migration path against the
#                           real, already-populated production table.
#   section_label         <- Chunk.section_label (nullable on the dataclass)
#   source_url            <- Chunk.attribution.source_url
#   published_date        <- Chunk.attribution.published_date (nullable)
#   retrieval_timestamp   <- Chunk.attribution.retrieval_timestamp
#   start_seconds          <- Chunk.start_seconds (nullable, unused by every
#                           current source — DECISIONS #58)
#   end_seconds            <- Chunk.end_seconds (nullable, same as above)
#   embedding              <- set by this pipeline, NULL until embedded
#   embedding_model        <- set by this pipeline, NULL until embedded
#   embedded_at            <- set by this pipeline, NULL until embedded
CHUNK_COLUMNS: tuple[str, ...] = (
    "chunk_id",
    "doc_type",
    "chunk_text",
    "content_hash",
    "section_label",
    "source_url",
    "published_date",
    "retrieval_timestamp",
    "start_seconds",
    "end_seconds",
    "embedding",
    "embedding_model",
    "embedded_at",
)

# text-embedding-3-small's real output dimensionality, verified live against
# the actual OpenAI API on 2026-08-21 (one real embedding call, response
# vector length checked) — not assumed from memory. See DECISIONS #71.
# Single source of truth lives in app.embeddings.client (imported above);
# not redefined here to avoid the two constants silently drifting apart.
