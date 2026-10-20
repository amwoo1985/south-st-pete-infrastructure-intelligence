"""Tests for app/embeddings/client.py.

No live OpenAI call in this suite (the one live call for this project is
the exploratory dimensionality check in DECISIONS #71, not a test). This
is the first time this repo mocks the `openai` SDK rather than HTTP
traffic (DECISIONS #23 established `responses`-based HTTP-fixture mocking
for the crawlers) — the approach here is different by necessity: our own
code takes an `openai.OpenAI` *client instance* as a parameter
(dependency-injected, never constructed inside app.embeddings.client), so
tests substitute a fake client object whose `.embeddings.create` is a
plain `unittest.mock.MagicMock` — no need to intercept HTTP at all, since
the SDK boundary IS the seam this code depends on.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx2
import openai
import pytest

from app.embeddings.client import (
    EMBEDDING_DIMENSIONS,
    EmbeddingInputTooLargeError,
    EmbeddingValidationError,
    MAX_INPUT_CHARS,
    get_embedding,
)

_REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/embeddings")


def _fake_response(embedding: list) -> SimpleNamespace:
    return SimpleNamespace(data=[SimpleNamespace(embedding=embedding)])


def _fake_client(**embeddings_create_kwargs) -> MagicMock:
    client = MagicMock()
    client.embeddings.create = MagicMock(**embeddings_create_kwargs)
    return client


def _valid_embedding() -> list[float]:
    return [0.1] * EMBEDDING_DIMENSIONS


# --- Happy path --------------------------------------------------------


def test_get_embedding_returns_validated_vector():
    client = _fake_client(return_value=_fake_response(_valid_embedding()))

    result = get_embedding(client, "some chunk text")

    assert result == _valid_embedding()
    client.embeddings.create.assert_called_once()
    _, kwargs = client.embeddings.create.call_args
    assert kwargs["input"] == "some chunk text"
    assert kwargs["model"] == "text-embedding-3-small"


# --- Input-size guard: no API call at all -------------------------------


def test_input_too_large_raises_before_calling_api():
    client = _fake_client()
    text = "x" * (MAX_INPUT_CHARS + 1)

    with pytest.raises(EmbeddingInputTooLargeError):
        get_embedding(client, text)

    client.embeddings.create.assert_not_called()


def test_input_at_the_limit_does_not_raise():
    client = _fake_client(return_value=_fake_response(_valid_embedding()))
    text = "x" * MAX_INPUT_CHARS

    get_embedding(client, text)  # must not raise

    client.embeddings.create.assert_called_once()


# --- Retry classification: retryable errors -----------------------------


def test_rate_limit_error_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("app.embeddings.client.time.sleep", lambda _seconds: None)
    rate_limit_response = httpx2.Response(status_code=429, request=_REQUEST)
    client = _fake_client(
        side_effect=[
            openai.RateLimitError("rate limited", response=rate_limit_response, body=None),
            _fake_response(_valid_embedding()),
        ]
    )

    result = get_embedding(client, "text", max_attempts=3)

    assert result == _valid_embedding()
    assert client.embeddings.create.call_count == 2


def test_rate_limit_error_exhausts_retries_and_raises_original(monkeypatch):
    monkeypatch.setattr("app.embeddings.client.time.sleep", lambda _seconds: None)
    rate_limit_response = httpx2.Response(status_code=429, request=_REQUEST)
    err = openai.RateLimitError("rate limited", response=rate_limit_response, body=None)
    client = _fake_client(side_effect=[err, err, err])

    with pytest.raises(openai.RateLimitError):
        get_embedding(client, "text", max_attempts=3)

    assert client.embeddings.create.call_count == 3


def test_connection_error_is_retried(monkeypatch):
    monkeypatch.setattr("app.embeddings.client.time.sleep", lambda _seconds: None)
    client = _fake_client(
        side_effect=[
            openai.APIConnectionError(request=_REQUEST),
            _fake_response(_valid_embedding()),
        ]
    )

    result = get_embedding(client, "text", max_attempts=3)

    assert result == _valid_embedding()
    assert client.embeddings.create.call_count == 2


# --- Retry classification: non-retryable errors -------------------------


def test_bad_request_error_raises_immediately_without_retry(monkeypatch):
    monkeypatch.setattr("app.embeddings.client.time.sleep", lambda _seconds: None)
    bad_request_response = httpx2.Response(status_code=400, request=_REQUEST)
    client = _fake_client(
        side_effect=openai.BadRequestError(
            "bad request", response=bad_request_response, body=None
        )
    )

    with pytest.raises(openai.BadRequestError):
        get_embedding(client, "text", max_attempts=3)

    client.embeddings.create.assert_called_once()


def test_authentication_error_raises_immediately_without_retry(monkeypatch):
    monkeypatch.setattr("app.embeddings.client.time.sleep", lambda _seconds: None)
    auth_response = httpx2.Response(status_code=401, request=_REQUEST)
    client = _fake_client(
        side_effect=openai.AuthenticationError("bad key", response=auth_response, body=None)
    )

    with pytest.raises(openai.AuthenticationError):
        get_embedding(client, "text", max_attempts=3)

    client.embeddings.create.assert_called_once()


# --- Output validation: model output is untrusted input ------------------


def test_wrong_dimension_raises_validation_error():
    client = _fake_client(return_value=_fake_response([0.1] * 42))

    with pytest.raises(EmbeddingValidationError, match="1536"):
        get_embedding(client, "text")


def test_non_list_embedding_raises_validation_error():
    client = _fake_client(return_value=_fake_response("not-a-list"))

    with pytest.raises(EmbeddingValidationError, match="list"):
        get_embedding(client, "text")


def test_empty_data_raises_validation_error():
    client = _fake_client(return_value=SimpleNamespace(data=[]))

    with pytest.raises(EmbeddingValidationError, match="exactly 1"):
        get_embedding(client, "text")


def test_non_numeric_values_raise_validation_error():
    embedding = [0.1] * (EMBEDDING_DIMENSIONS - 1) + ["oops"]
    client = _fake_client(return_value=_fake_response(embedding))

    with pytest.raises(EmbeddingValidationError, match="non-numeric"):
        get_embedding(client, "text")


def test_non_finite_value_raises_validation_error():
    embedding = [0.1] * (EMBEDDING_DIMENSIONS - 1) + [float("nan")]
    client = _fake_client(return_value=_fake_response(embedding))

    with pytest.raises(EmbeddingValidationError, match="non-finite"):
        get_embedding(client, "text")


def test_infinity_value_raises_validation_error():
    embedding = [0.1] * (EMBEDDING_DIMENSIONS - 1) + [float("inf")]
    client = _fake_client(return_value=_fake_response(embedding))

    with pytest.raises(EmbeddingValidationError, match="non-finite"):
        get_embedding(client, "text")


def test_malformed_data_entry_raises_validation_error_not_attribute_error():
    # data[0] has no .embedding attribute at all — the guarded access in
    # _validate_embedding must turn this into EmbeddingValidationError,
    # not let a raw AttributeError escape to the caller.
    client = _fake_client(return_value=SimpleNamespace(data=[SimpleNamespace()]))

    with pytest.raises(EmbeddingValidationError, match="malformed response shape"):
        get_embedding(client, "text")
