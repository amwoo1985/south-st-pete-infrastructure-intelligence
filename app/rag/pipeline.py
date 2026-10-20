"""Thin end-to-end composition of app.rag.retrieval + app.rag.generation
— one `query: str -> AnswerResult` call, Phase F+G (PLAN.md).

Owns no logic of its own beyond wiring: `retrieve()` decides what
survives threshold/dedup/budget filtering, `generate_answer()` decides
what to do with whatever `retrieve()` returned (including the empty
case — see that module's not-in-corpus short-circuit). This module just
calls one after the other and packages both results together so a
caller (a script, or later api-layer's endpoint) gets full visibility
into both the retrieval decision and the generation decision from one
call, for logging/debugging/citation-display purposes.
"""

from __future__ import annotations

from dataclasses import dataclass

import openai
import psycopg

from app.rag.generation import GenerationResult, generate_answer
from app.rag.retrieval import RetrievalResult, retrieve


@dataclass(frozen=True)
class AnswerResult:
    retrieval: RetrievalResult
    generation: GenerationResult


def answer_query(
    conn: psycopg.Connection,
    client: openai.OpenAI,
    query: str,
    *,
    retrieve_kwargs: dict | None = None,
    generate_kwargs: dict | None = None,
) -> AnswerResult:
    """Retrieves, then generates. Propagates whatever either step raises
    — no exception handling of its own beyond what retrieve()/
    generate_answer() already do (both already fail loud with typed
    exceptions per .claude/rules/rag.md; swallowing anything here would
    just re-hide it one layer up)."""
    retrieval_result = retrieve(conn, client, query, **(retrieve_kwargs or {}))
    generation_result = generate_answer(
        client, query, retrieval_result.chunks, **(generate_kwargs or {})
    )
    return AnswerResult(retrieval=retrieval_result, generation=generation_result)
