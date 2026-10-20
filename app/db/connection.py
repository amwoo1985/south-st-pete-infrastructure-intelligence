"""Connection helper for the local dev Postgres+pgvector stack that
deploy-infra brought up (DECISIONS #68-70) — docker-compose's ``db``
service, credentials in the gitignored repo-root ``.env`` as
``LOCAL_DB_*`` (DECISIONS #69). Connects to ``127.0.0.1`` by default
(host-run callers) or ``LOCAL_DB_HOST`` when set (containerized
``api``/``worker`` services connect to the Compose service name ``db``
instead — see docker-compose.yml).

This module owns *connecting* to that already-running, already-initialized
database. It does not create the ``vector`` extension (DECISIONS #70 —
that's the init-script's job) and does not manage docker-compose/infra/
(deploy-infra's territory, not touched here).
"""

from __future__ import annotations

import os

import psycopg
from dotenv import load_dotenv
from pgvector.psycopg import register_vector

# Loads .env into the process environment if present. Safe to call more
# than once (idempotent no-op after the first successful load) and safe in
# a deployed context where real env vars are already set some other way —
# load_dotenv() never overwrites an already-set env var by default.
load_dotenv()

_REQUIRED_VARS = ("LOCAL_DB_USER", "LOCAL_DB_PASSWORD", "LOCAL_DB_NAME")


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill in the "
            "LOCAL_DB_* values (see DECISIONS #69) before using app.db."
        )
    return value


def get_connection() -> psycopg.Connection:
    """Opens one connection to the local dev DB, with the pgvector adapter
    registered (via pgvector.psycopg — see DECISIONS #71) so a ``vector``
    column can be written as a plain Python ``list[float]``. On read, a
    ``vector`` column comes back as a ``pgvector.vector.Vector`` object,
    NOT a plain ``list[float]`` — call ``.to_list()`` to get one (confirmed
    live against the real ``chunks`` table by app/rag/retrieval.py, the
    first code in this repo to actually SELECT a ``vector`` column back
    out rather than only ever writing it; see DECISIONS #92).

    Autocommit is OFF (psycopg's default): callers control their own
    commit()/rollback() boundaries. This matters for the embed-and-insert
    pipeline's partial-failure handling (app/embeddings/pipeline.py) and
    for tests that roll back a whole test's writes in one transaction.
    """
    for name in _REQUIRED_VARS:
        _env(name)

    conn = psycopg.connect(
        # Defaults to 127.0.0.1 for every host-run caller (pytest, dev
        # scripts, `uvicorn --reload` run from the host) — unchanged
        # behavior, no .env edit required. Containerized services
        # (docker-compose.yml's `api`/`worker`) override this to `db`,
        # the Compose service name, since inside a container 127.0.0.1
        # means the container itself, not the `db` service.
        host=os.environ.get("LOCAL_DB_HOST", "127.0.0.1"),
        port=os.environ.get("LOCAL_DB_PORT", "5432"),
        user=_env("LOCAL_DB_USER"),
        password=_env("LOCAL_DB_PASSWORD"),
        dbname=_env("LOCAL_DB_NAME"),
    )
    register_vector(conn)
    return conn
