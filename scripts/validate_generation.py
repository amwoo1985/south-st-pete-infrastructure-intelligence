"""Phase F+G live end-to-end validation: real hand-picked queries run
through the FULL retrieve() + generate_answer() pipeline
(app/rag/pipeline.py) against the live 342-chunk corpus, using real
OpenAI API calls (embeddings AND chat completions) and the real local
Postgres.

NOT part of the pytest suite (scripts/, not tests/) -- pytest mocks every
OpenAI call per this project's test-mocking precedent (DECISIONS #72);
this script is the one deliberately-real end-to-end check, the Phase F+G
analog of scripts/validate_retrieval.py's Phase E precedent.

What this checks, concretely (see this round's DECISIONS.md entry for
the full write-up):
  - The DECISIONS #73 Pinellas HCD ambiguous-program finding (a correct
    on-topic program at rank #1, 0.776, but the specific intended
    similarly-named program at rank #7, 0.68) -- confirms retrieval keeps
    both distinct programs in context and generation names them
    separately rather than silently picking one.
  - A genuine "not in corpus" case -- something that needs real Granicus
    meeting-audio transcript content, which does not exist yet (no
    transcription jobs have run against this corpus) -- confirms the
    system says so honestly instead of hallucinating from general
    knowledge.
  - A handful of DECISIONS #73's already-validated on-topic queries, to
    confirm the RELEVANCE_THRESHOLD=0.5 carryover still holds now that
    dedup + token-budget logic sits in front of it, not just the bare
    cosine-similarity check DECISIONS #73 originally ran.
  - Real token usage (from the API's own `usage` field, not estimated)
    and the real dollar cost of this validation run.

Run: python scripts/validate_generation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import openai
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.connection import get_connection
from app.rag.pipeline import answer_query
from app.rag.retrieval import RELEVANCE_THRESHOLD

load_dotenv()

# gpt-4o-mini real per-token pricing, verified live against
# developers.openai.com/api/docs/pricing this session (see this round's
# DECISIONS.md entry): $0.15 / 1M input tokens, $0.60 / 1M output tokens.
GPT_4O_MINI_INPUT_PRICE_PER_TOKEN = 0.15 / 1_000_000
GPT_4O_MINI_OUTPUT_PRICE_PER_TOKEN = 0.60 / 1_000_000

# text-embedding-3-small's published rate (DECISIONS #73): $0.02 / 1M
# tokens. Query-embedding cost below is estimated from char count (same
# ~4 chars/token heuristic as app/embeddings/client.py's MAX_INPUT_CHARS
# -- no tokenizer dependency added here either, consistent with that
# precedent), since the embeddings API response used by this pipeline
# doesn't surface a `usage` field the way the chat completion does.
EMBEDDING_PRICE_PER_TOKEN = 0.02 / 1_000_000
CHARS_PER_TOKEN_ESTIMATE = 4

QUERIES = [
    # --- DECISIONS #73 named finding: re-check live through the full
    # pipeline (threshold + dedup + token budget + generation), not just
    # the bare cosine-similarity check that originally surfaced it.
    # Deliberately phrased WITHOUT naming a specific program (unlike
    # DECISIONS #73's original "...Home Repair Loan Program" query,
    # which already biased toward one of the two real, similarly-named
    # programs) -- this is the actual ambiguous case: does the system
    # surface both "Home Repair Loan Program" and "Hurricane Home Repair
    # Program" rather than silently picking one?
    "What home repairs are eligible under Pinellas County's home repair "
    "assistance programs?",
    # --- Genuine "not in corpus" case: needs real Granicus meeting-audio
    # transcript content (what a Council member said during public
    # comment), which does not exist in this corpus -- no transcription
    # job has completed yet (Tier-1.5 worker built but not run at scale,
    # DECISIONS #84-91). Legistar only has structured agenda-item data,
    # never a transcript of what was actually said.
    "What specific concerns did residents raise during public comment "
    "when the St. Petersburg City Council discussed the Duke Energy "
    "franchise agreement?",
    # --- Regression sanity: 3 of DECISIONS #73's already-validated
    # on-topic hits, re-run through the full pipeline.
    "What is the SHIP income limit for a household of 4?",
    "What is the plan for deep energy efficiency retrofits of municipal "
    "facilities?",
    "When is SPHA public housing rent due each month, and what happens "
    "if it's late?",
]


def main() -> None:
    client = openai.OpenAI()
    conn = get_connection()

    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_query_chars = 0
    generation_calls = 0

    for query in QUERIES:
        print(f"\n{'=' * 100}\nQuery: {query!r}\n{'=' * 100}")
        total_query_chars += len(query)

        result = answer_query(conn, client, query)

        r = result.retrieval
        print(
            f"  retrieval: {r.candidates_considered} candidates considered, "
            f"{r.above_threshold} above threshold (>={RELEVANCE_THRESHOLD}), "
            f"{r.deduped_out} deduped out, {len(r.chunks)} kept "
            f"(~{r.context_tokens_estimate} context tokens est.)"
        )
        for c in r.chunks:
            print(
                f"    sim={c.similarity:.4f} [{c.doc_type}] {c.section_label!r} "
                f"chunk_id={c.chunk_id}"
            )

        g = result.generation
        print(f"  generation: not_in_corpus={g.not_in_corpus} model={g.model}")
        print(f"    answer: {g.answer}")
        print(f"    citations: {g.citations}")
        if g.invalid_citations_dropped:
            print(f"    DROPPED invalid/hallucinated citations: {g.invalid_citations_dropped}")

        if g.model is not None:
            generation_calls += 1
        if g.prompt_tokens is not None:
            total_prompt_tokens += g.prompt_tokens
        if g.completion_tokens is not None:
            total_completion_tokens += g.completion_tokens

    embedding_cost = (total_query_chars / CHARS_PER_TOKEN_ESTIMATE) * EMBEDDING_PRICE_PER_TOKEN
    chat_input_cost = total_prompt_tokens * GPT_4O_MINI_INPUT_PRICE_PER_TOKEN
    chat_output_cost = total_completion_tokens * GPT_4O_MINI_OUTPUT_PRICE_PER_TOKEN
    total_cost = embedding_cost + chat_input_cost + chat_output_cost

    print(f"\n{'=' * 100}\nCOST SUMMARY\n{'=' * 100}")
    print(f"  {len(QUERIES)} queries run, {generation_calls} actually called gpt-4o-mini "
          f"({len(QUERIES) - generation_calls} short-circuited at the not-in-corpus check)")
    print(f"  chat completion prompt tokens (real, from API usage): {total_prompt_tokens}")
    print(f"  chat completion completion tokens (real, from API usage): {total_completion_tokens}")
    print(f"  query embedding cost (chars/4 estimate): ${embedding_cost:.6f}")
    print(f"  chat input cost (real tokens x $0.15/1M): ${chat_input_cost:.6f}")
    print(f"  chat output cost (real tokens x $0.60/1M): ${chat_output_cost:.6f}")
    print(f"  TOTAL real cost this run: ${total_cost:.6f}")

    conn.close()


if __name__ == "__main__":
    main()
