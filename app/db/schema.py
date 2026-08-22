"""Phase E schema: one ``chunks`` table holding both a chunk's content/
attribution and its embedding vector. See DECISIONS #71 for the full
one-table-vs-two-tables reasoning; short version: a Chunk and its
embedding are always 1:1 in this pipeline (no multi-vector-per-chunk
strategy exists or is planned), so a join buys nothing and a two-table
design would double the write surface (and therefore the partial-failure
surface .claude/rules/data.md's cleanup rule is about) for no benefit.

This module owns the ``chunks`` table's DDL only. It does NOT create the
``vector`` extension (infra/local-db/init-pgvector.sql + DECISIONS #70
already guarantee that by the time any app code runs) and does NOT touch
docker-compose/infra/ (deploy-infra's territory).
"""

from __future__ import annotations

import psycopg

from app.embeddings.client import EMBEDDING_DIMENSIONS

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

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    doc_type TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    section_label TEXT,
    source_url TEXT NOT NULL,
    published_date DATE,
    retrieval_timestamp TIMESTAMPTZ NOT NULL,
    start_seconds DOUBLE PRECISION,
    end_seconds DOUBLE PRECISION,
    embedding VECTOR({EMBEDDING_DIMENSIONS}),
    embedding_model TEXT,
    embedded_at TIMESTAMPTZ
);
"""

# HNSW, not ivfflat — decided for THIS corpus's scale (hundreds to low
# thousands of chunks across all 8 sources, per the task framing), not as a
# generic "HNSW is better" default. See DECISIONS #71 for the full
# reasoning; short version:
#   - ivfflat's `lists` parameter needs to be sized against a representative
#     row count to be meaningful, and the index should be (re)built after
#     the data is loaded, ideally re-tuned/reindexed as the table grows
#     meaningfully — awkward for a corpus that's being built incrementally
#     across 8 sources over several days, not loaded once upfront.
#   - HNSW has no training-data/lists-sizing step — it builds incrementally
#     as rows are inserted/updated, and degrades gracefully as the corpus
#     grows, with no reindex-on-growth requirement.
#   - The build-time/memory cost that's normally HNSW's tradeoff against
#     ivfflat is a non-issue at hundreds-to-low-thousands of rows — trivial
#     either way at this N. The real differentiator at this scale is
#     operational simplicity (no lists tuning, no growth-triggered
#     reindex), which favors HNSW outright here.
# vector_cosine_ops: matches the cosine-distance `<=>` operator the
# retrieval layer (Phase F, not built yet) will use — OpenAI's
# text-embedding-3-small embeddings are documented as unit-normalized, so
# cosine and dot-product rank identically, but cosine is the more legible
# choice for a similarity THRESHOLD (bounded [0, 2] distance / [-1, 1]
# similarity) than an unbounded inner product.
_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);
"""


def apply_schema(conn: psycopg.Connection) -> None:
    """Idempotent: safe to call on every process start (CREATE TABLE/INDEX
    IF NOT EXISTS). Commits its own transaction — call this once, before
    any chunk inserts, not per-chunk."""
    with conn.cursor() as cur:
        cur.execute(_CREATE_TABLE_SQL)
        cur.execute(_CREATE_INDEX_SQL)
    conn.commit()
