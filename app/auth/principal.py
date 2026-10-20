"""Who is making this request (DECISIONS #164).

``get_principal`` is the FastAPI dependency every protected route will use
(wired onto routes in layer B4). It:

1. reads ``Authorization: Bearer <access token>``,
2. verifies the token with Cognito's keys (app/auth/tokens.py), before
   touching the database, so anonymous or junk requests never open a
   Postgres connection,
3. looks the person up in our own ``users`` table by the token's ``sub``,
   on every request, on its own short-lived connection, and refuses anyone
   without an active row.

Step 3 is the kill switch. A Cognito access token stays valid until it
expires (up to 60 minutes here). Local checks can't see a Cognito sign-out
or revocation. Setting ``users.disabled_at`` cuts someone off on their very
next request.

Responses: 401 when there's no usable token (with ``WWW-Authenticate:
Bearer``), 403 when the token is genuine but the person has no active
account here, 503 when authentication isn't configured, Cognito's keys
can't be reached, or the database is down. The detail shown to callers is deliberately generic; the
specific reason is logged. Routes must not return the Principal wholesale
(it carries ``is_platform_admin``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

import psycopg
from fastapi import HTTPException, Request, status

from app.auth.tokens import AuthNotConfiguredError, AuthUnavailableError, TokenError, verify_access_token
from app.db.connection import get_connection

logger = logging.getLogger("auth")

USER_COLUMNS = ("user_id", "cognito_sub", "email", "display_name", "is_platform_admin", "disabled_at")


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    cognito_sub: str
    email: str
    display_name: str | None
    is_platform_admin: bool


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Sign-in required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _unavailable() -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Sign-in is not available right now.")


def load_active_principal(conn, cognito_sub: str) -> Principal | None:
    """The active (not disabled) user with this Cognito sub, or None."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(USER_COLUMNS)} FROM users WHERE cognito_sub = %s AND disabled_at IS NULL",
            (cognito_sub,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    record = dict(zip(USER_COLUMNS, row))
    return Principal(
        user_id=record["user_id"],
        cognito_sub=record["cognito_sub"],
        email=record["email"],
        display_name=record["display_name"],
        is_platform_admin=record["is_platform_admin"],
    )


def verified_claims(request: Request) -> dict:
    """The verified claims of the request's bearer token, or the matching
    HTTP error. Touches no database."""
    token = _bearer_token(request)
    if token is None:
        raise _unauthorized()
    try:
        return verify_access_token(token)
    except AuthNotConfiguredError as exc:
        logger.error("authentication not configured: %s", exc)
        raise _unavailable()
    except AuthUnavailableError as exc:
        logger.error("authentication unavailable: %s", exc)
        raise _unavailable()
    except TokenError as exc:
        logger.info("rejected token: %s", exc)
        raise _unauthorized()


def get_principal(request: Request) -> Principal:
    """Identity only, for routes that don't touch tenant data. Routes that
    read or write the database use app.auth.context.get_auth_context, which
    shares one connection and tells the database who's acting."""
    claims = verified_claims(request)

    # Only a genuine, current token gets this far. Own connection, closed
    # immediately, so no idle transaction is left on the route's connection.
    try:
        conn = get_connection()
        try:
            principal = load_active_principal(conn, claims["sub"])
        finally:
            conn.close()
    except psycopg.Error as exc:
        logger.error("account lookup failed (database unavailable): %s", exc)
        raise _unavailable()
    if principal is None:
        logger.info("valid token but no active account for sub=%s", claims["sub"])
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Your account doesn't have access to this app.")
    return principal
