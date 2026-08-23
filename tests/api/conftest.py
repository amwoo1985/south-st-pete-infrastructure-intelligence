"""Fixtures for tests exercising app/api/ over FastAPI's TestClient.

Every test in this directory requires the docker-compose `db` service to
be up, same as tests/db/ (tests/db/conftest.py's docstring) — app.api.main
imports app.db.connection/app.db.schema directly and its lifespan applies
real schema on startup, and several routes (`/sources`, `/documents/
upload`) do real reads/writes against the real `chunks`/`document_uploads`
tables rather than a mocked DB, consistent with this repo's "no precedent
for mocking Postgres" convention (tests/db/conftest.py).

OpenAI is still always mocked (DECISIONS #72/#90) — no test in this
directory makes a real OpenAI network call; `client` fixture below patches
app.api.dependencies._openai_client with a MagicMock, and
tests/api/test_query.py additionally patches app.api.query.answer_query
directly so the real RAG pipeline's OpenAI calls never fire either.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.main import app


@pytest.fixture(scope="session")
def client():
    # TestClient as a context manager triggers app.api.main's lifespan
    # (startup: apply_schema + apply_document_uploads_schema against the
    # real local DB; shutdown: nothing, no teardown needed) exactly like a
    # real `uvicorn` run would.
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def mock_openai_client(monkeypatch):
    """Replaces app.api.dependencies' module-level OpenAI client singleton
    with a MagicMock for the duration of one test — belt-and-suspenders
    alongside directly mocking answer_query()/chunk-processing in
    individual test modules, so no code path in these tests can
    accidentally make a real network call to OpenAI even if a future
    change stops mocking answer_query directly."""
    mock_client = MagicMock()
    monkeypatch.setattr("app.api.dependencies._openai_client", mock_client)
    return mock_client
