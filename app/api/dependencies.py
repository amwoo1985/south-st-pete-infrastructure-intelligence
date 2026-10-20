"""Shared FastAPI dependencies for every route in app/api/.

DB connection strategy: one plain connection per request, opened and closed
around the request, with no pool. Load is modest, and connect() is cheap
next to the OpenAI round-trips. It's also what makes the tenant-identity
model safe: protected routes use app.auth.context.get_auth_context, which
sets the caller's identity (app.user_id) at session level on that request's
own connection (DECISIONS #166). **Adding a connection pooler (psycopg_pool,
PgBouncer, RDS Proxy) requires switching that to per-transaction
set_config(..., true) first**, or one tenant's identity could leak into
another's request.
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
