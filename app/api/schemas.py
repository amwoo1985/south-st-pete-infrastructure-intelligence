"""Pydantic request/response models for every route in app/api/. Every
endpoint has an explicit response model here — no bare dicts returned
from a route (.claude/rules/data.md's typed-marshalling spirit, applied
at the API boundary: FastAPI serializes these, nothing hand-formats
JSON)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# A real question for this domain (a CBA/grant/meeting-record question)
# is a sentence or few, not a document — this corpus's own chunk sizes
# top out around ~2,300 chars at the 95th percentile (DECISIONS #92).
# 2,000 chars is generous headroom above any real question while still
# rejecting an obvious misuse case (someone pasting a whole document into
# the question field) with a fast, local, structured 422 — before ever
# spending a DB round-trip or an OpenAI call. This is a separate, tighter
# guard than app/embeddings/client.py's MAX_INPUT_CHARS=24,000 embedding-
# input ceiling (which EmbeddingInputTooLargeError already guards, mapped
# to 400 in app/api/query.py) — that ceiling exists to protect the
# embeddings API call itself; this one exists to keep normal usage of
# this endpoint fast and cheap to reject when clearly out of scope for
# "a question."
QUESTION_MAX_LENGTH = 2000


class QueryRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=1,
        max_length=QUESTION_MAX_LENGTH,
        description="A natural-language question to answer from the indexed corpus.",
    )

    @field_validator("question")
    @classmethod
    def _question_not_blank(cls, value: str) -> str:
        # min_length=1 alone lets a whitespace-only string like " "
        # through (len 1, not blank by Pydantic's count) — this catches
        # that case explicitly rather than passing a blank question all
        # the way to retrieve()/embed_and_insert.
        if not value.strip():
            raise ValueError("question must not be blank")
        return value


class Citation(BaseModel):
    """Full attribution for one cited chunk — never a bare chunk_id, per
    the orchestrator's directive: a client gets something immediately
    useful without a second round-trip to GET /sources/{doc_id}."""

    chunk_id: str
    doc_type: str
    section_label: str | None
    source_url: str
    published_date: date | None


class QueryResponse(BaseModel):
    answer: str
    citations: list[Citation]
    not_in_corpus: bool


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db: Literal["ok", "error"]


class SourceRecordResponse(BaseModel):
    """A chunks-table row minus its embedding vector (no reason to ship
    1536 floats to a client) — app/api/sources.py."""

    chunk_id: str
    doc_type: str
    chunk_text: str
    section_label: str | None
    source_url: str
    published_date: date | None
    retrieval_timestamp: datetime
    start_seconds: float | None
    end_seconds: float | None
    embedding_model: str | None
    embedded_at: datetime | None


class DocumentUploadResponse(BaseModel):
    """Shape returned by POST /documents/upload in every branch (already-
    completed short-circuit at HTTP 200, new-or-retry processing kickoff
    at HTTP 202 — see app/api/documents.py)."""

    file_hash: str
    status: Literal["processing", "completed"]
    original_filename: str
    chunk_count: int | None = None
    uploaded_at: datetime
    completed_at: datetime | None = None
    previous_failure_reason: str | None = Field(
        default=None,
        description=(
            "Set only when this response is retrying a file_hash whose prior attempt "
            "ended in status='failed' — the stored reason for that prior failure, "
            "surfaced so a client doesn't have to re-fail identically before learning "
            "why (api-review pre-commit finding, DECISIONS #114)."
        ),
    )
