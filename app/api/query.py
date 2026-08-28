"""POST /query — wraps app.rag.pipeline.answer_query. This module's main
job is the exception -> HTTP status mapping below: every typed exception
answer_query()/retrieve()/generate_answer() can raise gets a deliberate,
distinct status code, never folded into a bare 500 (see each except
clause's comment for the reasoning) — with ONE deliberate exception to
"every exception maps to an HTTPException", see UngroundedAnswerError
below. Internal exception text/API details are never put in a response
body — every HTTPException detail below is a static string; the real
exception is only ever logged server-side.

Ordering note: UngroundedAnswerError is caught BEFORE its parent class
GenerationValidationError (Python matches the first applicable except
clause in source order, and except clauses for a subclass must precede
the parent's or the parent's clause would shadow it). Every other clause
here is a sibling exception type (EmbeddingInputTooLargeError vs
EmbeddingValidationError vs the openai.* family), so their relative order
doesn't affect correctness — they're ordered instead by response-code
severity for readability. The openai.OpenAIError catch-all must come
after openai.RateLimitError/APITimeoutError/APIConnectionError, since
those are all its subclasses.

UngroundedAnswerError -> clean QueryResponse, NOT 502 (DECISIONS #144):
this used to map to HTTP 502 like every other upstream-contract
violation. DECISIONS #143 (rag-pipeline) reworded the corrective-retry
prompt to push harder against a premature not_in_corpus concession,
which changed what exhaustion of that retry can mean — it's no longer
only "something is broken," it can now also mean "genuinely tried hard
and still couldn't produce a citation the code trusts." A 502 also
implicitly invites "try again," which is not an honest suggestion here —
generation runs at GENERATION_TEMPERATURE=0 and DECISIONS #143 reproduced
this exact failure mode as deterministic for a fixed query, so retrying
would reproduce the same outcome. `.claude/rules/rag.md` is explicit that
an unanswerable question gets a response that "says so explicitly," not
an error page — so this returns `not_in_corpus=True`, not an
HTTPException.

Deliberately NOT the same text as `generate_answer()`'s own
NOT_IN_CORPUS_ANSWER (api-review pre-commit finding, this round): that
string means retrieve() found NOTHING above the relevance threshold —
zero candidate chunks. This path is different — retrieve() DID find
candidates, the model just couldn't produce a citation the code trusts
after a real corrective retry, and DECISIONS #143 disclosed its own
residual gaps in that trust check (a short-answer edge case in the
citation-overlap heuristic, no stemming). Byte-identical text here would
make a genuinely-answerable question that trips that heuristic edge case
read exactly like "the records are silent on this" — a false negative
with no way for anyone (Amber included) to tell the two apart without
grepping server logs. `_UNGROUNDED_ANSWER` below is a separate, honest
string: still calm and non-alarming (no 502, no "error" framing), but
says "inconclusive," not "not covered" — a real distinction a careful
reader can act on (e.g. try rephrasing, the same fix that resolved
DECISIONS #143's own root-cause case). Server-side visibility is
unchanged either way: still logged at ERROR below (and again inside
generate_answer() itself).
"""

from __future__ import annotations

import logging

import openai
from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import get_db_conn, get_openai_client
from app.api.schemas import Citation, QueryRequest, QueryResponse
from app.embeddings.client import EmbeddingInputTooLargeError, EmbeddingValidationError
from app.rag.generation import GenerationValidationError, UngroundedAnswerError
from app.rag.pipeline import answer_query

# See module docstring's UngroundedAnswerError section for why this is
# its own string, not app.rag.generation.NOT_IN_CORPUS_ANSWER.
_UNGROUNDED_ANSWER = (
    "This system found material that might be related to this question, but "
    "could not produce a source citation it trusts enough to answer "
    "confidently. This is inconclusive, not a confirmation that the records "
    "are silent on this topic — try rephrasing the question."
)

logger = logging.getLogger("api.query")

router = APIRouter()

# Explicit column list, no SELECT * (.claude/rules/data.md) — only what
# Citation (app/api/schemas.py) actually needs.
_CITATION_COLUMNS = ("chunk_id", "doc_type", "section_label", "source_url", "published_date")


@router.post("/query", response_model=QueryResponse)
def query(
    req: QueryRequest,
    conn=Depends(get_db_conn),
    client: openai.OpenAI = Depends(get_openai_client),
) -> QueryResponse:
    """Sync route (no `async def`): every call in the chain below —
    psycopg, the OpenAI SDK's sync client — is itself synchronous, so
    there is nothing to `await`. FastAPI runs a sync `def` route in a
    worker thread automatically, which is the correct behavior here
    rather than wrapping already-sync calls in async machinery that buys
    nothing."""
    try:
        result = answer_query(conn, client, req.question)
    except UngroundedAnswerError as exc:
        # Exhausted the bounded corrective retry (DECISIONS #143) without
        # ever producing a citation the code trusts. See module docstring
        # for why this returns not_in_corpus=True instead of a 502, and
        # why the answer text is `_UNGROUNDED_ANSWER` (this module), NOT
        # generate_answer()'s own NOT_IN_CORPUS_ANSWER — a real zero-
        # retrieval miss and "found something, couldn't trust a citation
        # for it" are different situations and must not read identically.
        # Logged at ERROR (unchanged) so this is still visible server-side
        # even though the client sees a calm, non-error answer shape.
        logger.error(
            "query %r: ungrounded-answer contract violation after exhausted "
            "corrective retries — returning not_in_corpus=True with the "
            "distinct 'inconclusive' wording, not a 502 (DECISIONS #144): %s",
            req.question,
            exc,
        )
        return QueryResponse(answer=_UNGROUNDED_ANSWER, citations=[], not_in_corpus=True)
    except GenerationValidationError as exc:
        # Chat completion response wasn't valid JSON / didn't match the
        # required shape — same upstream-contract-violation family as
        # above, just a different failure point in generation.
        logger.error("query %r: generation output validation failed: %s", req.question, exc)
        raise HTTPException(
            status_code=502,
            detail="The language model returned an invalid response. Please try again.",
        ) from exc
    except EmbeddingValidationError as exc:
        # Embeddings API returned a malformed response shape — same
        # "upstream sent something we can't trust" family, different
        # upstream call.
        logger.error("query %r: embedding response validation failed: %s", req.question, exc)
        raise HTTPException(
            status_code=502,
            detail="The embedding service returned an invalid response. Please try again.",
        ) from exc
    except EmbeddingInputTooLargeError as exc:
        # This one IS the client's fault (their question text was too
        # long to embed) — 400, not 502/500.
        logger.warning("query %r: question too long to embed: %s", req.question, exc)
        raise HTTPException(
            status_code=400,
            detail="Question is too long to process. Please shorten it and try again.",
        ) from exc
    except openai.RateLimitError as exc:
        # answer_query()'s own retry/backoff already ran and exhausted
        # its attempts before this reached us.
        logger.warning("query %r: rate limited after retries exhausted: %s", req.question, exc)
        raise HTTPException(
            status_code=429,
            detail="The service is currently rate-limited. Please try again shortly.",
        ) from exc
    except (openai.APITimeoutError, openai.APIConnectionError) as exc:
        logger.error(
            "query %r: upstream timeout/connection failure after retries exhausted: %s",
            req.question,
            exc,
        )
        raise HTTPException(
            status_code=504,
            detail="The request to the language model timed out. Please try again.",
        ) from exc
    except openai.OpenAIError as exc:
        # Anything else from the OpenAI SDK (auth failure, a malformed
        # request on THIS app's side, etc) — a config/programming problem
        # on this app's side, not the end user's; generic message,
        # full detail logged server-side only.
        logger.error("query %r: unexpected OpenAI error: %s", req.question, exc)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again later.",
        ) from exc
    except Exception:
        logger.exception("query %r: unexpected error", req.question)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again later.",
        )

    citations = _lookup_citations(conn, result.generation.citations)
    return QueryResponse(
        answer=result.generation.answer,
        citations=citations,
        not_in_corpus=result.generation.not_in_corpus,
    )


def _lookup_citations(conn, chunk_ids: tuple[str, ...]) -> list[Citation]:
    """Looks up full attribution for each cited chunk_id so the client
    gets it in one round-trip (orchestrator's directive) rather than a
    bare chunk_id list. Preserves `chunk_ids`' own order (the model's
    citation order), not whatever order the DB happens to return rows in.
    """
    if not chunk_ids:
        return []

    placeholders = ", ".join(["%s"] * len(chunk_ids))
    sql = f"SELECT {', '.join(_CITATION_COLUMNS)} FROM chunks WHERE chunk_id IN ({placeholders})"
    with conn.cursor() as cur:
        cur.execute(sql, tuple(chunk_ids))
        rows = cur.fetchall()

    by_chunk_id = {row[0]: row for row in rows}
    citations: list[Citation] = []
    for chunk_id in chunk_ids:
        row = by_chunk_id.get(chunk_id)
        if row is None:
            # Should not happen — these chunk_ids came from retrieve()'s
            # own read of `chunks` moments earlier in this same request —
            # but a concurrent delete between then and now isn't
            # impossible. Log and skip rather than 500ing a response that
            # otherwise has a real, valid answer.
            logger.warning(
                "query: cited chunk_id=%s not found in chunks at citation-lookup "
                "time (skipped from response)",
                chunk_id,
            )
            continue
        _, doc_type, section_label, source_url, published_date = row
        citations.append(
            Citation(
                chunk_id=chunk_id,
                doc_type=doc_type,
                section_label=section_label,
                source_url=source_url,
                published_date=published_date,
            )
        )
    return citations
