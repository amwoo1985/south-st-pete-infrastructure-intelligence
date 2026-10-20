"""Reuses tests/db/conftest.py's real-Postgres, transactional-rollback
fixtures (DECISIONS #72) for app/rag/retrieval.py and app/rag/pipeline.py
tests. `tests/rag/` is a sibling of `tests/db/`, not a subdirectory, so
pytest's directory-scoped conftest discovery won't find `db_conn`/
`_schema_ready` here on its own — importing them into this module's
namespace is what makes pytest see them as fixtures for this directory
too, same trick as any other cross-directory fixture reuse.
"""

from __future__ import annotations

from tests.db.conftest import _schema_ready, db_conn  # noqa: F401
