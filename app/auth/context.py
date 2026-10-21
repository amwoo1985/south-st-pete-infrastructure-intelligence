"""One connection per authenticated request, carrying the caller's identity
into the database (DECISIONS #166).

``get_auth_context`` is the dependency every protected route uses (all API
routes except /health, DECISIONS #169):

1. verifies the bearer token, before any database work (no connection is
   opened for anonymous or junk requests),
2. opens the request's single connection,
3. loads the active ``users`` row (same rules as ``get_principal``),
4. sets the session variable ``app.user_id`` to that person, then COMMITS
   right away, so a later ``rollback()`` in the route can't erase it. The
   database's grant rules and audit triggers read this value; they never
   trust an actor column sent by the API,
5. yields ``AuthContext(principal, conn)`` and closes the connection after.

Pooling note: the identity is session-level. That's safe only because each
request gets its own connection and closes it. Introducing a connection
pooler requires switching to per-transaction ``set_config(..., true)``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

import psycopg
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer

from app.auth.principal import Principal, _unavailable, load_active_principal, verified_claims
from app.db.connection import get_connection

logger = logging.getLogger("auth")

# Declared only so /docs shows an Authorize button that sends the bearer
# token; the token itself is read and verified from the request by
# verified_claims(), not from this scheme (auto_error=False: never rejects).
_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthContext:
    principal: Principal
    conn: psycopg.Connection


def set_acting_user(conn: psycopg.Connection, principal: Principal) -> None:
    """Tells the database who's acting on this connection, durably for the
    connection's lifetime (committed, so rollbacks don't undo it)."""
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.user_id', %s, false)", (str(principal.user_id),))
    conn.commit()


def get_auth_context(request: Request, _scheme: object = Depends(_bearer_scheme)) -> Iterator[AuthContext]:
    claims = verified_claims(request)
    try:
        conn = get_connection()
    except psycopg.Error as exc:
        logger.error("could not open database connection: %s", exc)
        raise _unavailable()
    try:
        try:
            principal = load_active_principal(conn, claims["sub"])
        except psycopg.Error as exc:
            logger.error("account lookup failed (database unavailable): %s", exc)
            raise _unavailable()
        if principal is None:
            logger.info("valid token but no active account for sub=%s", claims["sub"])
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Your account doesn't have access to this app.")
        try:
            set_acting_user(conn, principal)
        except psycopg.Error as exc:
            logger.error("could not set acting user (database unavailable): %s", exc)
            raise _unavailable()
        yield AuthContext(principal=principal, conn=conn)
    finally:
        conn.close()
