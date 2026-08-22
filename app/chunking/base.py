"""Shared Chunk representation and deterministic chunk-ID scheme for every
content shape this project ingests (crawled HTML/PDF prose, tabular data,
and — later — Granicus meeting transcripts).

Binding rules this module exists to enforce (see .claude/rules/rag.md,
.claude/rules/data.md, PLAN.md Phase D, DECISIONS #58):

- Every Chunk carries full attribution passthrough (app.crawlers.base's
  Attribution) — chunking never re-derives or drops source_url/
  retrieval_timestamp/published_date.
- Chunk IDs are stable and deterministic, derived from source identity
  (URL + section/row/item key), never a random UUID or an incrementing
  counter — this is what makes re-running the chunker on the same crawled
  data idempotent (same input -> same ID -> upsert, not a duplicate row).
- A Chunk never carries empty text — an empty chunk is a bug in a
  source-specific chunker, not a legitimate output.

Scope: this module produces a normalized Chunk ready to be embedded later
(PLAN.md Phase E). It does not embed, store in pgvector, retrieve, or
generate — those are Phases E/F/G, out of scope here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.crawlers.base import Attribution

# Unit separator (ASCII 0x1F) — not a character that could plausibly appear
# in a URL, heading, or row key — joins chunk-identity parts before hashing
# so ("a", "bc") and ("ab", "c") can never collide the way naive "|"-joining
# could if a field itself happened to contain "|". See DECISIONS #58.
_ID_PART_SEPARATOR = "\x1f"


def make_chunk_id(*identity_parts: str) -> str:
    """Deterministic chunk ID: the sha256 hex digest of the identity parts,
    truncated to 32 hex chars (128 bits — collision-negligible at this
    corpus's scale, and short enough to be a practical DB primary key).

    Derived ONLY from stable identity fields (source URL + section/row/item
    identity) — deliberately NEVER from the chunk's own text content, and
    never from a random UUID or an incrementing counter. Re-running the
    chunker against the same crawled data always produces the same ID for
    the same logical chunk (same source URL, same section/row/item key),
    even if upstream prose is lightly re-worded on a later crawl — which is
    exactly what "idempotent re-run" (PLAN.md Phase D, .claude/rules/
    data.md) requires: a re-run upserts by ID instead of duplicating rows.
    See DECISIONS #58.
    """
    if not identity_parts:
        raise ValueError("make_chunk_id() requires at least one identity part")
    joined = _ID_PART_SEPARATOR.join(identity_parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class Chunk:
    """One normalized, embedding-ready unit of text plus its provenance.

    Every crawled source shape this codebase currently produces (HTML/PDF
    prose, agenda-item structure, tabular AMI data) is normalized into this
    one representation before Phase E's embedding step.

    - chunk_id: see make_chunk_id() — stable and deterministic, not random,
      not an incrementing counter.
    - doc_type: discriminator string (e.g. "legistar_agenda_item",
      "stpete_program_detail", "ami_threshold_table",
      "pinellas_cf_grant", "arpa_funding_allocation") so retrieval/citation
      code later can distinguish shapes without inspecting text.
    - text: the chunk's actual content, ready to embed. Never empty — see
      __post_init__.
    - section_label: a human-readable label for this chunk's position
      within its source document (an agenda item number, an <h2> heading,
      a program name, ...). Not used for retrieval matching — makes a
      citation readable ("Agenda Item 10, Dec 16 2025 BCC meeting" instead
      of just a bare URL) and helps debugging. Nullable because not every
      source shape has a natural human-readable position (see
      .claude/rules/data.md's nullable-over-sentinel rule — an empty
      string here would be a magic "no label" sentinel).
    - attribution: full passthrough of the source item's Attribution
      (app.crawlers.base) — source_url, retrieval_timestamp,
      published_date. Chunking never re-derives or drops this.
    - start_seconds / end_seconds: nullable timestamp-range fields, unused
      by every current source (no crawler in this codebase produces
      transcript data yet — PLAN.md Phase C / Granicus hasn't been built).
      They exist so a future transcript-with-timestamps source can
      populate a chunk's position within meeting audio without a schema
      change to this dataclass. This is a documented extension point, not
      a built feature — see DECISIONS #58. Do not populate these from any
      current source.
    """

    chunk_id: str
    doc_type: str
    text: str
    section_label: str | None
    attribution: Attribution
    start_seconds: float | None = None
    end_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.text or not self.text.strip():
            raise ValueError(
                f"Chunk {self.chunk_id!r} (doc_type={self.doc_type!r}) has empty text "
                "— a source-specific chunker produced a chunk with nothing to embed"
            )
