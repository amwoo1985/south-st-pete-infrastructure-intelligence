"""Organization, membership and support-access administration routes
(DECISIONS #166, #169).

These routes are thin on purpose. The rules about who may do what (only a
platform admin creates an organization, org admins grant roles, the last
admin can't leave, support access needs the customer's consent and lasts at
most 14 days, and so on) live in database triggers (migration 0005) that read
the signed-in person from ``app.user_id``. The routes never send an actor:
the triggers stamp ``created_by`` / ``granted_by`` / ``consented_by`` /
``revoked_by`` themselves. A route here only (1) shapes the request, (2)
runs the SQL as the signed-in person, and (3) turns a database refusal into a
plain HTTP answer. That means a bug in this file can't hand out more power
than the database allows.

Only reads need an authorization check in Python, because triggers guard
writes only: listing an organization's members or support grants needs the
caller to be an org admin (or point person, for members), asked of the
database via ``app_is_member`` (the same helper the triggers use).

Error mapping (``_db_error``): a refusal raised by one of our trigger
functions carries a sentence written for people (recognized because it comes
with a PL/pgSQL context; Postgres's own messages, such as "permission denied
for table x", have none) and is passed along as the ``detail``:
insufficient_privilege -> 403, check_violation -> 409, invalid input -> 422.
Everything else gets a fixed sentence: table CHECK and unique constraints
(409), bad characters (422), serialization conflicts (409), connection
trouble (503), and anything unexpected (logged, generic 500).

Writes that name a person by email (add a member, grant support access)
check the caller's right to act in the org BEFORE looking the email up, and
refuse with one uniform 403, so a signed-in stranger can't use these routes
to learn which emails have accounts, who is vendor staff, or which
organizations exist.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response, status
from psycopg import errors as pgerr
from datetime import timedelta, timezone

from pydantic import AwareDatetime, BaseModel, Field, field_validator

from app.auth.context import AuthContext, get_auth_context

logger = logging.getLogger("api.admin")

router = APIRouter(tags=["organizations"])

ROLES = ("org_admin", "point_person", "contributor", "budget_owner", "authorized_approver")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

ORG_COLUMNS = ("org_id", "legal_name", "display_name", "status", "created_at")
MEMBER_COLUMNS = ("membership_id", "user_id", "email", "display_name", "role", "granted_at")
SUPPORT_COLUMNS = ("grant_id", "support_user_id", "email", "reason", "starts_at", "expires_at", "revoked_at")

_NOT_ALLOWED = "You aren't allowed to do that."
_CANT_ADD = "That person can't be added. Check the email address and that they have an active account."


# --- request / response models --------------------------------------------


def _no_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError("must not contain NUL characters")
    return value


def _normalize_email(value: str) -> str:
    email = value.strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 320:
        raise ValueError("must be an email address")
    return email


class OrganizationCreate(BaseModel):
    legal_name: str = Field(min_length=1, max_length=300)
    display_name: str | None = Field(default=None, max_length=300)

    @field_validator("legal_name", "display_name")
    @classmethod
    def _not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return v
        _no_nul(v)
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()


class OrganizationResponse(BaseModel):
    org_id: UUID
    legal_name: str
    display_name: str | None
    status: str
    created_at: datetime


class MembershipCreate(BaseModel):
    email: str
    role: str

    @field_validator("email")
    @classmethod
    def _email_ok(cls, v: str) -> str:
        return _normalize_email(v)

    @field_validator("role")
    @classmethod
    def _known_role(cls, v: str) -> str:
        if v not in ROLES:
            raise ValueError("role must be one of: " + ", ".join(ROLES))
        return v


class MembershipResponse(BaseModel):
    membership_id: UUID
    user_id: UUID
    email: str
    display_name: str | None
    role: str
    granted_at: datetime


class SupportGrantCreate(BaseModel):
    support_user_email: str
    reason: str = Field(min_length=1, max_length=1000)
    expires_at: AwareDatetime

    @field_validator("support_user_email")
    @classmethod
    def _email_ok(cls, v: str) -> str:
        return _normalize_email(v)

    @field_validator("expires_at")
    @classmethod
    def _within_fourteen_days(cls, v):
        now = datetime.now(timezone.utc)
        if v <= now:
            raise ValueError("must be in the future")
        if v > now + timedelta(days=14):
            raise ValueError("must be no more than 14 days from now")
        return v

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, v: str) -> str:
        _no_nul(v)
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()


class SupportGrantResponse(BaseModel):
    grant_id: UUID
    support_user_id: UUID
    email: str
    reason: str
    starts_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


class CreatedId(BaseModel):
    id: UUID


# --- helpers ---------------------------------------------------------------


def _people_sentence(exc: psycopg.Error) -> str | None:
    """The refusal's message, only if one of our own trigger functions wrote
    it for people (it carries a PL/pgSQL context and no constraint name).
    Postgres's own messages, e.g. "permission denied for table x", have no
    such context and are never shown."""
    message = (exc.diag.message_primary or "").strip()
    if not message or not exc.diag.context or exc.diag.constraint_name is not None or "app." in message:
        return None
    return message


def _db_error(exc: psycopg.Error, what: str) -> HTTPException:
    """Turns a database refusal into an HTTP answer (see module docstring)."""
    sentence = _people_sentence(exc)
    if isinstance(exc, pgerr.InsufficientPrivilege):
        return HTTPException(status.HTTP_403_FORBIDDEN, sentence or _NOT_ALLOWED)
    if isinstance(exc, pgerr.UniqueViolation):
        return HTTPException(status.HTTP_409_CONFLICT, "That already exists.")
    if isinstance(exc, pgerr.CheckViolation):
        return HTTPException(status.HTTP_409_CONFLICT, sentence or "That request breaks one of the rules for this record.")
    if isinstance(exc, pgerr.DataError):
        return HTTPException(422, sentence or "One of the values isn't acceptable.")
    if isinstance(exc, (pgerr.SerializationFailure, pgerr.DeadlockDetected)):
        return HTTPException(status.HTTP_409_CONFLICT, "Another change was in progress. Please try again.")
    if isinstance(exc, (pgerr.OperationalError, pgerr.InterfaceError)):
        logger.error("%s: database unavailable: %s", what, exc)
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The service is unavailable right now. Please try again.")
    logger.error("%s: unexpected database error: %s", what, exc)
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "An internal error occurred. Please try again later.")


def _rollback(conn) -> None:
    try:
        conn.rollback()
    except psycopg.Error as exc:  # a dead connection must not mask the mapped error
        logger.error("rollback failed: %s", exc)


def _run(ctx: AuthContext, what: str, fn):
    """Runs fn(cursor) inside the request's transaction; commits on success,
    rolls back and maps the error on a database refusal."""
    try:
        with ctx.conn.cursor() as cur:
            result = fn(cur)
        ctx.conn.commit()
        return result
    except psycopg.Error as exc:
        _rollback(ctx.conn)
        raise _db_error(exc, what)
    except Exception:
        _rollback(ctx.conn)
        raise


def _require_member(cur, ctx: AuthContext, org_id: UUID, roles: list[str]) -> None:
    """Reads are not guarded by triggers, so ask the database whether the
    caller holds one of these roles in the org. The answer is the same for an
    org that doesn't exist and one the caller doesn't belong to."""
    cur.execute("SELECT app_is_member(%s, %s, %s)", (org_id, ctx.principal.user_id, roles))
    if not cur.fetchone()[0]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, _NOT_ALLOWED)


def _active_user_id(cur, email: str) -> UUID:
    cur.execute("SELECT user_id FROM users WHERE email = %s AND disabled_at IS NULL", (email,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(422, _CANT_ADD)
    return row[0]


# --- organizations ---------------------------------------------------------


@router.get("/orgs", response_model=list[OrganizationResponse])
def list_orgs(ctx: AuthContext = Depends(get_auth_context)) -> list[OrganizationResponse]:
    """Organizations the caller belongs to; a platform admin sees every
    organization's name and status (never its content)."""

    def work(cur):
        columns = ", ".join(f"o.{c}" for c in ORG_COLUMNS)
        cur.execute(
            f"SELECT {columns} FROM organizations o "
            "WHERE %s OR app_is_member(o.org_id, %s, %s) ORDER BY o.legal_name, o.org_id",
            (ctx.principal.is_platform_admin, ctx.principal.user_id, list(ROLES)),
        )
        return [OrganizationResponse(**dict(zip(ORG_COLUMNS, row))) for row in cur.fetchall()]

    return _run(ctx, "list orgs", work)


@router.post("/orgs", response_model=OrganizationResponse, status_code=status.HTTP_201_CREATED)
def create_org(body: OrganizationCreate, ctx: AuthContext = Depends(get_auth_context)) -> OrganizationResponse:
    """Platform admins only (enforced by the database)."""

    def work(cur):
        returning = ", ".join(ORG_COLUMNS)
        cur.execute(
            f"INSERT INTO organizations (legal_name, display_name) VALUES (%s, %s) RETURNING {returning}",
            (body.legal_name, body.display_name),
        )
        return OrganizationResponse(**dict(zip(ORG_COLUMNS, cur.fetchone())))

    return _run(ctx, "create org", work)


# --- memberships -----------------------------------------------------------


@router.get("/orgs/{org_id}/memberships", response_model=list[MembershipResponse])
def list_memberships(org_id: UUID, ctx: AuthContext = Depends(get_auth_context)) -> list[MembershipResponse]:
    """Active members. Org admins and point people only."""

    def work(cur):
        _require_member(cur, ctx, org_id, ["org_admin", "point_person"])
        cur.execute(
            "SELECT m.membership_id, m.user_id, u.email, u.display_name, m.role, m.granted_at "
            "FROM org_memberships m JOIN users u ON u.user_id = m.user_id "
            "WHERE m.org_id = %s AND m.revoked_at IS NULL ORDER BY u.email, m.role",
            (org_id,),
        )
        return [MembershipResponse(**dict(zip(MEMBER_COLUMNS, row))) for row in cur.fetchall()]

    return _run(ctx, "list memberships", work)


@router.post("/orgs/{org_id}/memberships", response_model=CreatedId, status_code=status.HTTP_201_CREATED)
def add_membership(org_id: UUID, body: MembershipCreate, ctx: AuthContext = Depends(get_auth_context)) -> CreatedId:
    """Gives an existing, active person a role in the org. Who may grant which
    role is decided by the database."""

    def work(cur):
        if not ctx.principal.is_platform_admin:  # a platform admin may appoint a first org admin
            _require_member(cur, ctx, org_id, ["org_admin", "point_person"])
        user_id = _active_user_id(cur, body.email)
        cur.execute(
            "INSERT INTO org_memberships (org_id, user_id, role) VALUES (%s, %s, %s) RETURNING membership_id",
            (org_id, user_id, body.role),
        )
        return CreatedId(id=cur.fetchone()[0])

    return _run(ctx, "add membership", work)


@router.delete("/orgs/{org_id}/memberships/{membership_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_membership(org_id: UUID, membership_id: UUID, ctx: AuthContext = Depends(get_auth_context)) -> Response:
    def work(cur):
        cur.execute(
            "UPDATE org_memberships SET revoked_at = now() WHERE membership_id = %s AND org_id = %s",
            (membership_id, org_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such membership in this organization.")

    _run(ctx, "revoke membership", work)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- support access --------------------------------------------------------


@router.get("/orgs/{org_id}/support-grants", response_model=list[SupportGrantResponse])
def list_support_grants(org_id: UUID, ctx: AuthContext = Depends(get_auth_context)) -> list[SupportGrantResponse]:
    """Vendor support access this org has granted, newest first. Org admins only."""

    def work(cur):
        _require_member(cur, ctx, org_id, ["org_admin"])
        cur.execute(
            "SELECT g.grant_id, g.support_user_id, u.email, g.reason, g.starts_at, g.expires_at, g.revoked_at "
            "FROM support_access_grants g JOIN users u ON u.user_id = g.support_user_id "
            "WHERE g.org_id = %s ORDER BY g.starts_at DESC, g.grant_id",
            (org_id,),
        )
        return [SupportGrantResponse(**dict(zip(SUPPORT_COLUMNS, row))) for row in cur.fetchall()]

    return _run(ctx, "list support grants", work)


@router.post("/orgs/{org_id}/support-grants", response_model=CreatedId, status_code=status.HTTP_201_CREATED)
def grant_support_access(
    org_id: UUID, body: SupportGrantCreate, ctx: AuthContext = Depends(get_auth_context)
) -> CreatedId:
    """The org's admin consents to a vendor staff member's temporary access
    (at most 14 days; the database enforces the limit and who may consent)."""

    def work(cur):
        _require_member(cur, ctx, org_id, ["org_admin"])
        support_user_id = _active_user_id(cur, body.support_user_email)
        cur.execute(
            "INSERT INTO support_access_grants (org_id, support_user_id, reason, expires_at) "
            "VALUES (%s, %s, %s, %s) RETURNING grant_id",
            (org_id, support_user_id, body.reason, body.expires_at),
        )
        return CreatedId(id=cur.fetchone()[0])

    return _run(ctx, "grant support access", work)


@router.delete("/orgs/{org_id}/support-grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_support_access(org_id: UUID, grant_id: UUID, ctx: AuthContext = Depends(get_auth_context)) -> Response:
    def work(cur):
        cur.execute(
            "UPDATE support_access_grants SET revoked_at = now() WHERE grant_id = %s AND org_id = %s",
            (grant_id, org_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such support grant in this organization.")

    _run(ctx, "revoke support access", work)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
