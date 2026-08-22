"""Thin wrapper around the OpenAI embeddings endpoint (openai==3.3.1,
DECISIONS #13) for text-embedding-3-small (DECISIONS #13, #71).

Binding rules this module exists to enforce (.claude/rules/rag.md):
  - Classify failures before retrying: a rate limit / transient network
    error is retry-with-backoff; a bad request is not — retrying a
    malformed request won't fix it.
  - Don't auto-retry forever — bounded attempts, then fail with a reason.
  - Mind input size limits — a chunk that's too large for the embedding
    model's context window gets a clear, explicit error at this boundary,
    not an opaque 400 from the API.
  - Model output is untrusted input — validate the returned embedding's
    shape before it goes anywhere near the database.
"""

from __future__ import annotations

import logging
import math
import time

import openai

logger = logging.getLogger("embeddings")

# DECISIONS #13 / #71 — verified live 2026-08-21 against the real API
# (see DECISIONS #71 for the verification call and its output length).
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536

# Conservative character-count safety net, not a real tokenizer (adding
# tiktoken as a new dependency for a single pre-flight length check wasn't
# judged worth it — see DECISIONS #71). text-embedding-3-small's real
# limit is 8191 tokens; ~4 chars/token is a commonly-cited conservative
# average for English prose, so this ceiling leaves real headroom before
# ever reaching the model's actual limit. This is a belt-and-suspenders
# check, not the primary chunk-sizing control — chunk sizing is each
# chunking module's own job (Phase D, DECISIONS #59-66).
MAX_INPUT_CHARS = 24_000

# Exceptions worth retrying: rate limits and transient network/server
# failures. Everything else (bad request, auth, not found, ...) is a
# config/programming problem retrying will not fix, so it is NOT in this
# set and is raised immediately instead — an explicit allowlist, not a
# blocklist, per rag.md's "classify before retrying."
_RETRYABLE_EXCEPTIONS = (
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
)


class EmbeddingInputTooLargeError(ValueError):
    """Raised before ever calling the API when input text exceeds
    MAX_INPUT_CHARS — see module docstring."""


class EmbeddingValidationError(ValueError):
    """Raised when the API's response doesn't have the shape this
    pipeline requires (right length, numeric values) — model output is
    untrusted input per .claude/rules/rag.md."""


def get_embedding(
    client: openai.OpenAI,
    text: str,
    *,
    model: str = EMBEDDING_MODEL,
    max_attempts: int = 3,
    base_backoff_seconds: float = 1.0,
) -> list[float]:
    """Returns a validated embedding vector for `text`. Raises (never
    silently returns something malformed):
      - EmbeddingInputTooLargeError before calling the API, if `text` is
        too long (see MAX_INPUT_CHARS).
      - The original openai exception, after exhausting max_attempts, for
        retryable failures (rate limit / transient network / 5xx).
      - The original openai exception immediately (no retry) for anything
        else (bad request, auth, etc).
      - EmbeddingValidationError if the API returns 200 but the payload
        doesn't have the expected shape.
    """
    if len(text) > MAX_INPUT_CHARS:
        raise EmbeddingInputTooLargeError(
            f"input text is {len(text)} chars, over the {MAX_INPUT_CHARS}-char "
            "safety ceiling for text-embedding-3-small's 8191-token limit "
            "(see app/embeddings/client.py MAX_INPUT_CHARS) — normalize/"
            "shorten the source chunk rather than sending it as-is"
        )

    attempt = 0
    while True:
        attempt += 1
        try:
            response = client.embeddings.create(input=text, model=model)
            break
        except _RETRYABLE_EXCEPTIONS as exc:
            if attempt >= max_attempts:
                logger.error(
                    "embedding call failed after %d attempts (retryable: %s): %s",
                    attempt,
                    type(exc).__name__,
                    exc,
                )
                raise
            delay = base_backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                "embedding call attempt %d/%d failed (%s), retrying in %.1fs",
                attempt,
                max_attempts,
                type(exc).__name__,
                delay,
            )
            time.sleep(delay)
        except openai.OpenAIError as exc:
            # Not in _RETRYABLE_EXCEPTIONS: a bad request, auth failure,
            # etc — retrying will not fix this, fail immediately with the
            # original error rather than masking it behind retry noise.
            logger.error(
                "embedding call failed, not retrying (%s): %s", type(exc).__name__, exc
            )
            raise

    return _validate_embedding(response, expected_dimensions=EMBEDDING_DIMENSIONS)


def _validate_embedding(response, *, expected_dimensions: int) -> list[float]:
    """Model output is untrusted input (.claude/rules/rag.md) — check
    shape before this ever reaches a SQL query."""
    data = getattr(response, "data", None)
    if not data or len(data) != 1:
        raise EmbeddingValidationError(
            f"expected exactly 1 embedding in the response, got {len(data) if data else 0}"
        )

    try:
        embedding = data[0].embedding
    except AttributeError as exc:
        raise EmbeddingValidationError(
            f"expected response.data[0] to have an .embedding attribute, "
            f"got a malformed response shape: {exc}"
        ) from exc

    if not isinstance(embedding, list):
        raise EmbeddingValidationError(
            f"expected response embedding to be a list, got {type(embedding).__name__}"
        )
    if len(embedding) != expected_dimensions:
        raise EmbeddingValidationError(
            f"expected a {expected_dimensions}-dimension embedding, got {len(embedding)}"
        )
    if not all(isinstance(v, (int, float)) for v in embedding):
        raise EmbeddingValidationError("embedding contains non-numeric values")
    if not all(math.isfinite(v) for v in embedding):
        raise EmbeddingValidationError(
            "embedding contains a non-finite value (NaN or Infinity) — "
            "would corrupt cosine-distance math in the vector column"
        )

    return embedding
