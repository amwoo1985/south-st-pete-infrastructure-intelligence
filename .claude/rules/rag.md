# RAG / LLM Rules

Binding for any code touching retrieval, embeddings, prompts, or generation. Specialists (`rag-pipeline`) and reviewers (`rag-review`) load this before acting.

## Prompting

- Prompts are static. Never interpolate user-controlled text directly into the instruction portion of a prompt — that's a prompt-injection hole. Reference data (allowed categories, valid values) goes in as a fixed, controlled list.
- The model answers only from retrieved context. If the context doesn't contain the answer, the response says so explicitly — it never falls through to the model's general knowledge.
- Every answer traces to cited source chunk(s).

## Retrieval

- Filter by a relevance/similarity threshold before assembling context — never blind top-k.
- Deduplicate near-duplicate chunks before they enter the context window.
- Cap total context tokens explicitly; know the number and why.
- Chunk size/overlap must fit the domain — CBA and grant-guideline clauses are semantically dense; don't split a clause mid-thought with naive fixed-length chunking without checking.

## Treat model output as untrusted input

- Validate every field the model returns — type, range, enum membership — before it touches the database or gets rendered.
- The model will occasionally return malformed output (wrong date format, invented category, number-as-string). Parse defensively.

## Failure handling

- Classify failures before retrying: a rate limit is retry-with-backoff; a bad request or malformed prompt is not — retrying won't fix it.
- Don't auto-retry a failing extraction/generation forever. Fail with a reason, surface it, let the caller decide whether to retry.
- Mind input size limits (vision/context window caps) — normalize or downscale input before sending, don't let it fail silently at the API boundary.
