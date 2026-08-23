"""Shared FastAPI dependencies for every route in app/api/.

DB connection strategy (api-layer design decision, this round, stated
explicitly per the orchestrator's directive rather than picked silently):
plain per-request open/close via get_db_conn() below, NOT a connection
pool. Reasoning: this is a solo-operator civic tool, not a multi-tenant
SaaS — real expected concurrent request volume is one person (Amber),
occasionally a handful of coalition members, hitting a handful of
endpoints. psycopg's connect() overhead on localhost/RDS at this volume
is negligible next to the OpenAI network round-trip /query already pays
on every call. A pool (e.g. psycopg_pool) adds a second stateful thing to
size, tune, and reason about failure modes for (pool exhaustion, stale
pooled connections surviving a DB restart) for a benefit that doesn't
show up until concurrent request volume is far higher than this tool
will ever realistically see. If real concurrent load ever materializes,
revisit — but pre-optimizing for it now would be solving a problem this
project doesn't have.
"""

from __future__ import annotations

import openai

from app.db.connection import get_connection

# Module-level singleton, constructed once at import time — safe to share
# across requests (the OpenAI SDK client holds no per-request state; every
# call in this codebase already treats it this way, e.g. each script in
# scripts/ builds one client and reuses it for the whole run). Unlike the
# DB connection below, there is no per-request transaction boundary an
# OpenAI client needs isolated per request, so there is no equivalent
# reason to open/close one per request — doing so would only add
# construction overhead (and lose HTTP connection-pool reuse) for no
# correctness benefit.
_openai_client = openai.OpenAI()


def get_openai_client() -> openai.OpenAI:
    return _openai_client


def get_db_conn():
    """FastAPI dependency: opens one psycopg connection per request via
    app.db.connection.get_connection(), and closes it when the request
    finishes (success or error) via this generator's `finally` — see
    module docstring for why per-request open/close over a pool.

    Autocommit is OFF (app/db/connection.py's default) — a route that
    writes (app/api/documents.py's upload pre-check/upsert) must call
    conn.commit() itself; closing an uncommitted connection rolls back,
    which is the safe default for the read-only routes (/query,
    /sources/{doc_id}, which never call commit() at all)."""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()
