"""Threshold-filtered, deduplicated, token-budgeted retrieval over the
`chunks` table (app/db/schema.py) — Phase F (PLAN.md).

Binding rules this module exists to enforce (.claude/rules/rag.md):
  - Filter by a relevance/similarity threshold before assembling context
    — never blind top-k. See RELEVANCE_THRESHOLD below.
  - Deduplicate near-duplicate chunks before they enter the context
    window. See DEDUP_SIMILARITY_THRESHOLD below.
  - Cap total context tokens explicitly; know the number and why. See
    MAX_CONTEXT_TOKENS below.
  - Retrieved chunks keep full citation data (source_url, published_date,
    section_label, doc_type) intact for app/rag/generation.py to cite.

Query flow (`retrieve()`):
  1. Embed the query (app.embeddings.client.get_embedding — same
     retry/validation contract as every other embedding call in this
     codebase; a rate limit retries with backoff, a bad request raises
     immediately, model output is shape-validated before it's ever used).
  2. Pull a generous CANDIDATE_POOL_SIZE nearest-neighbor set from
     pgvector via cosine distance (`<=>`, matching the HNSW index's
     vector_cosine_ops — DECISIONS #71, same operator
     scripts/validate_retrieval.py already uses). This LIMIT is a
     pragmatic bound on how many rows are even worth examining, NOT the
     relevance decision itself — accept/reject is threshold + dedup +
     token budget, never "the first K by rank" (rag.md's "never blind
     top-k").
  3. Drop every candidate below RELEVANCE_THRESHOLD.
  4. Deduplicate: walking the surviving candidates in similarity-
     descending order (the SQL query's own ORDER BY), drop any candidate
     whose cosine similarity to an already-kept chunk's embedding is
     >= DEDUP_SIMILARITY_THRESHOLD (near-duplicate content, e.g.
     templated grant-listing boilerplate rows — DECISIONS #73's
     "template-near-duplicate sibling rows" finding). The higher-ranked
     (more query-relevant) chunk of a near-duplicate pair is always the
     one kept, since candidates are walked in descending-similarity
     order and only ever compared against already-kept (= higher- or
     equal-ranked) chunks.
  5. Cap total context tokens: walking the deduplicated, similarity-
     ranked list, accumulate an estimated token count and stop including
     further chunks once the next one would exceed MAX_CONTEXT_TOKENS.
     The single highest-ranked surviving chunk is always included even
     if it alone exceeds the budget (never return zero chunks when at
     least one genuinely relevant one exists) — every chunk after the
     first is strictly budget-gated.

Deliberately NOT a fixed "return top N chunks" — this is what resolves
DECISIONS #73's named Pinellas HCD finding (a correct on-topic program at
rank #1, 0.776 similarity, but the specific intended similarly-named
program at rank #7, 0.68 similarity — both above the 0.5 threshold, both
real, distinct programs, not a near-duplicate pair). A large-enough
CANDIDATE_POOL_SIZE means rank #7 is examined at all (scripts/
validate_retrieval.py's LIMIT 3 never would have surfaced it);
threshold-then-budget (not a small top-k) means it survives into context
alongside rank #1 rather than being silently cut off — both distinct
programs reach the generation prompt, and app/rag/generation.py's
grounding-contract system prompt explicitly instructs the model to name
each distinct item separately when this happens, rather than this module
building a reranker/disambiguator to guess which one the user meant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import openai
import psycopg

from app.db.schema import CHUNK_COLUMNS
from app.embeddings.client import get_embedding

logger = logging.getLogger("rag.retrieval")

# --- Tunables (see module docstring + DECISIONS #73 for the evidence) ------

# DECISIONS #73's live 342-chunk evidence: 7 hand-picked on-topic queries
# scored 0.63-0.83 cosine similarity against their real target chunks; a
# known off-corpus negative control's best (still-wrong) match scored
# 0.42-0.46. 0.5 sits in the real gap between those two populations.
# Re-validated live against this finished retrieval+generation pipeline
# in scripts/validate_generation.py before being accepted for real use —
# not reused unchecked from the earlier manual sanity check.
RELEVANCE_THRESHOLD = 0.5

# OpenAI's text-embedding-3-small vectors are documented unit-normalized
# (see app/db/schema.py's HNSW-index comment), so a plain dot product
# between two chunk embeddings already equals cosine similarity — no
# separate normalization step needed for the chunk-to-chunk comparison
# below.
#
# 0.97 is a deliberately conservative (very high) bar — this only
# collapses near-identical text (templated boilerplate rows differing in
# a few fields, e.g. DECISIONS #73's Pinellas CF "template-near-duplicate
# sibling rows"), not merely topically-similar-but-distinct chunks. The
# Pinellas HCD rank-1/rank-7 pair (0.776 and 0.68 similarity TO THE
# QUERY, two genuinely different programs) must NOT collapse under this
# rule — confirmed live in scripts/validate_generation.py that their
# chunk-to-chunk similarity sits well below 0.97.
DEDUP_SIMILARITY_THRESHOLD = 0.97

# How many nearest-neighbor rows to pull from pgvector before applying
# the threshold/dedup/budget filters above. NOT the relevance decision
# itself (see module docstring) — sized to comfortably include DECISIONS
# #73's rank-#7 Pinellas HCD finding (5-6x margin) without pulling a
# large fraction of the whole 342-chunk corpus on every query.
CANDIDATE_POOL_SIZE = 40

# No tokenizer dependency added for this estimate — the same heuristic
# and the same reasoning DECISIONS #71/#74 already established (and
# rag-review already signed off on) for app/embeddings/client.py's
# MAX_INPUT_CHARS: ~4 chars/token is a commonly-cited conservative
# average for English prose. Kept consistent with that precedent rather
# than introducing a second, different sizing convention here.
CHARS_PER_TOKEN_ESTIMATE = 4

# gpt-4o-mini's real context window is 128,000 tokens, verified live
# against developers.openai.com/api/docs/models/gpt-4o-mini this session
# (same live-verification discipline as DECISIONS #71's embedding
# dimensionality check and DECISIONS #86's Whisper pricing/limit check).
# 6,000 tokens of retrieved context is a deliberate small fraction of
# that ceiling (~4.7%), not a budget squeezed against the model's real
# limit:
#   - Cost: gpt-4o-mini's real input rate is $0.15/1M tokens (verified
#     live against developers.openai.com/api/docs/pricing this session)
#     — 6,000 context tokens costs ~$0.0009/query before the system
#     prompt/query/output are even added. Trivial at this project's real
#     query volume.
#   - Chunk-count headroom: this corpus's real chunk sizes (live-queried
#     from the 342-row `chunks` table this session) run ~831 chars
#     average (~208 est. tokens), 462 chars median (~115 est. tokens),
#     2,288 chars at the 95th percentile (~572 est. tokens). A 6,000-
#     token budget comfortably fits double digits of average/median-
#     sized chunks, or a healthy handful of unusually large ones —
#     comfortably enough that the Pinellas HCD rank-1 (0.776) + rank-7
#     (0.68) pair both fit together with room to spare, without any real
#     risk of silently truncating a legitimately-relevant chunk off the
#     end at this corpus's actual size distribution.
#   - Deliberately NOT sized near the 128K ceiling: a much larger budget
#     buys nothing here — threshold+dedup already narrow candidates to a
#     genuinely-relevant handful per query at this corpus's size — and
#     only costs more per call and gives the model more surface area to
#     mis-cite across, for no accuracy benefit.
MAX_CONTEXT_TOKENS = 6000


@dataclass(frozen=True)
class RetrievedChunk:
    """One chunk that survived threshold + dedup + token-budget
    filtering, with full citation data intact (.claude/rules/rag.md:
    every generated answer traces to cited source chunks)."""

    chunk_id: str
    doc_type: str
    chunk_text: str
    section_label: str | None
    source_url: str
    published_date: date | None
    similarity: float


@dataclass(frozen=True)
class RetrievalResult:
    query: str
    chunks: tuple[RetrievedChunk, ...]
    candidates_considered: int
    above_threshold: int
    deduped_out: int
    context_tokens_estimate: int

    @property
    def is_empty(self) -> bool:
        """True when nothing survived threshold filtering — the
        `.claude/rules/rag.md` "not in corpus" trigger. app/rag/
        generation.py decides what to do about it (skip the generation
        call entirely, see its module docstring); this module only
        reports the fact."""
        return len(self.chunks) == 0


# The `embedding` column comes back from psycopg (with pgvector's
# register_vector applied — app/db/connection.py) as a `pgvector.vector.
# Vector` object, NOT a plain `list[float]` as app/db/connection.py's own
# docstring currently describes (confirmed live against the real
# 342-chunk table this session: `type(row[embedding_idx])` is
# `pgvector.vector.Vector`, which needs `.to_list()` to get plain floats
# — that docstring claim appears to predate this module, the first code
# to actually SELECT the embedding column back out rather than only ever
# writing it. Flagged to the orchestrator; not fixed here since it's
# app/db/connection.py's docstring, outside this file's diff, and
# doesn't change any behavior this module relies on).
_SELECT_SQL = (
    f"SELECT {', '.join(CHUNK_COLUMNS)}, "
    "1 - (embedding <=> %s::vector) AS similarity "
    "FROM chunks WHERE embedding IS NOT NULL "
    "ORDER BY embedding <=> %s::vector LIMIT %s"
)

_COLUMN_INDEX: dict[str, int] = {name: i for i, name in enumerate(CHUNK_COLUMNS)}


def _to_float_list(vector_value) -> list[float]:
    if hasattr(vector_value, "to_list"):
        return vector_value.to_list()
    return list(vector_value)


def _dot(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two chunk embeddings, given both are
    unit-normalized (see DEDUP_SIMILARITY_THRESHOLD's comment above) —
    a plain dot product, no separate normalization step."""
    return sum(x * y for x, y in zip(a, b))


def retrieve(
    conn: psycopg.Connection,
    client: openai.OpenAI,
    query: str,
    *,
    threshold: float = RELEVANCE_THRESHOLD,
    dedup_threshold: float = DEDUP_SIMILARITY_THRESHOLD,
    candidate_pool_size: int = CANDIDATE_POOL_SIZE,
    max_context_tokens: int = MAX_CONTEXT_TOKENS,
) -> RetrievalResult:
    """Embeds `query`, runs pgvector cosine similarity search over
    `chunks`, and returns the threshold-filtered, deduplicated, token-
    budgeted survivors with full citation data intact.

    Raises whatever `get_embedding()` raises (a rate limit/timeout/
    connection error after exhausting its own retries, a bad-request
    error immediately, or `EmbeddingValidationError` for a malformed
    embedding) — this function adds no separate retry layer of its own
    for the embedding call, it reuses that module's already-established
    classify-before-retrying contract. Raises whatever the DB driver
    raises for a query failure (fail loud, no swallowing — mirrors
    app/embeddings/pipeline.py's treatment of connection-level DB
    failures as not-a-per-item problem).
    """
    query_vector = get_embedding(client, query)

    with conn.cursor() as cur:
        cur.execute(_SELECT_SQL, (query_vector, query_vector, candidate_pool_size))
        rows = cur.fetchall()

    candidates_considered = len(rows)

    # Rows already arrive ordered by ascending distance == descending
    # similarity (the SQL ORDER BY) — every loop below relies on that
    # ordering rather than re-sorting.
    above_threshold = [row for row in rows if row[-1] is not None and row[-1] >= threshold]

    kept_rows: list[tuple] = []
    kept_embeddings: list[list[float]] = []
    deduped_out = 0
    for row in above_threshold:
        embedding = _to_float_list(row[_COLUMN_INDEX["embedding"]])
        is_near_duplicate = any(
            _dot(embedding, kept_vec) >= dedup_threshold for kept_vec in kept_embeddings
        )
        if is_near_duplicate:
            deduped_out += 1
            logger.info(
                "dedup: dropped chunk_id=%s (cosine similarity to an "
                "already-kept, higher-ranked chunk >= %.2f)",
                row[_COLUMN_INDEX["chunk_id"]],
                dedup_threshold,
            )
            continue
        kept_rows.append(row)
        kept_embeddings.append(embedding)

    selected: list[RetrievedChunk] = []
    running_tokens = 0
    for row in kept_rows:
        chunk_text = row[_COLUMN_INDEX["chunk_text"]]
        estimated_tokens = max(1, len(chunk_text) // CHARS_PER_TOKEN_ESTIMATE)
        if selected and running_tokens + estimated_tokens > max_context_tokens:
            logger.info(
                "token budget reached at %d/%d surviving chunk(s) (~%d/%d "
                "tokens) for query %r — remaining above-threshold chunks "
                "not included this call",
                len(selected),
                len(kept_rows),
                running_tokens,
                max_context_tokens,
                query,
            )
            break
        running_tokens += estimated_tokens
        selected.append(
            RetrievedChunk(
                chunk_id=row[_COLUMN_INDEX["chunk_id"]],
                doc_type=row[_COLUMN_INDEX["doc_type"]],
                chunk_text=chunk_text,
                section_label=row[_COLUMN_INDEX["section_label"]],
                source_url=row[_COLUMN_INDEX["source_url"]],
                published_date=row[_COLUMN_INDEX["published_date"]],
                similarity=row[-1],
            )
        )

    return RetrievalResult(
        query=query,
        chunks=tuple(selected),
        candidates_considered=candidates_considered,
        above_threshold=len(above_threshold),
        deduped_out=deduped_out,
        context_tokens_estimate=running_tokens,
    )
