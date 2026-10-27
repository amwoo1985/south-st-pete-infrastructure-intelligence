"""Invitations: how people join an organization (DECISIONS #170-#173).

Inviting grants nothing. An org admin or point person creates a pending
invitation; the person gets a role only when they accept it. The rules (who
may invite whom, the daily caps, what accepting requires) live in the database
functions of migration 0006; these routes call them as the signed-in person
and translate their refusals.

``POST /orgs/{org_id}/invitations`` sequence, and why it is in this order:

1. ``invite_begin`` (database): is the caller allowed to invite this role here,
   and under the caps? It then takes the sender's and the organization's locks
   and checks again. The transaction stays open until step 3 commits, so
   concurrent requests can't all pass the caps and each create a Cognito
   account. Nothing outside the database is touched before this passes, so a
   stranger can't make us create Cognito accounts.
2. Cognito: find the account by email, or create it WITHOUT sending anything.
   The account's ``sub`` comes only from Cognito's reply, never from the
   request.
3. ``invite_user`` (database) records the invitation, using that ``sub``.
4. Only after the database has committed: if the invitation was created and the
   person has not signed in yet, Cognito sends the invitation email. If that
   send fails the invitation still stands; revoking it and inviting again
   retries the email.

The answer is always the same ``202 {"status": "invited"}`` whether the person
is new, existing, already a member, disabled or vendor staff. The differences
that remain (DECISIONS #170, #173): 403 for a non-member, 422 for bad input,
429 over the cap, 502 when Cognito is unavailable (or has two accounts for an
address, or can't find one it says exists), the response time (a new person costs
more Cognito calls; this is not equalized), and a Cognito account in an unusable
state (disabled, password reset required, compromised, unverified email), which
gets no email and, if the person isn't in our database yet, no invitation row.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator

from app.api.admin import ROLES, _db_error, _no_nul, _require_member, _rollback, _run
from app.auth.cognito_admin import CognitoAdmin, CognitoRefused, CognitoUnavailable, get_cognito_admin
from app.auth.context import AuthContext, get_auth_context

logger = logging.getLogger("api.invitations")

router = APIRouter(tags=["invitations"])

# The same ASCII-only rule the database applies (migration 0006).
_EMAIL_RE = re.compile(r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$")

INVITATION_COLUMNS = ("invitation_id", "org_legal_name", "role", "invited_by_name", "expires_at")
ORG_INVITATION_COLUMNS = ("invitation_id", "email", "role", "invited_by_email", "invited_at", "expires_at", "stale")


class InviteRequest(BaseModel):
    email: str
    role: str
    display_name: str | None = Field(default=None, max_length=200)

    @field_validator("email")
    @classmethod
    def _email_ok(cls, v: str) -> str:
        email = v.strip().lower()
        if len(email) > 254 or not _EMAIL_RE.match(email):
            raise ValueError("must be an email address")
        return email

    @field_validator("role")
    @classmethod
    def _role_ok(cls, v: str) -> str:
        if v not in ROLES:
            raise ValueError("role must be one of: " + ", ".join(ROLES))
        return v

    @field_validator("display_name")
    @classmethod
    def _name_ok(cls, v: str | None) -> str | None:
        if v is None:
            return v
        _no_nul(v)
        if any(unicodedata.category(c) in ("Cc", "Cf") for c in v):
            raise ValueError("must not contain control or formatting characters")
        return v


class InviteResponse(BaseModel):
    status: str = "invited"


class MyInvitation(BaseModel):
    invitation_id: UUID
    org_legal_name: str
    role: str
    invited_by_name: str
    expires_at: datetime


class OrgInvitation(BaseModel):
    invitation_id: UUID
    email: str
    role: str
    invited_by_email: str
    invited_at: datetime
    expires_at: datetime
    # The sender no longer has the authority to grant this role, so it can't be
    # accepted; revoke it and invite again.
    stale: bool


def cognito_provider() -> Callable[[], CognitoAdmin]:
    """Dependency: a function that returns the Cognito client. It is called
    only after the caller has been authorized, so a non-member gets the same
    403 whether or not Cognito is configured."""
    return get_cognito_admin


@router.post("/orgs/{org_id}/invitations", response_model=InviteResponse, status_code=status.HTTP_202_ACCEPTED)
def invite(
    org_id: UUID,
    body: InviteRequest,
    ctx: AuthContext = Depends(get_auth_context),
    get_cognito: Callable[[], CognitoAdmin] = Depends(cognito_provider),
) -> InviteResponse:
    # 1. Authorization and caps, then the locks. The transaction stays open
    #    (and holds them) through steps 2 and 3.
    try:
        with ctx.conn.cursor() as cur:
            cur.execute("SELECT invite_begin(%s, %s)", (org_id, body.role))
    except psycopg.Error as exc:
        _rollback(ctx.conn)
        raise _db_error(exc, "invite begin")

    try:
        # 2. The Cognito account (no email yet).
        try:
            cognito = get_cognito()
            account = cognito.find_by_email(body.email)
            if account is None:
                account = cognito.create_without_message(body.email)
        except CognitoRefused:
            raise HTTPException(422, "That email address can't be invited.")
        except CognitoUnavailable:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Inviting people is not available right now. Please try again later.")
        usable = account.can_be_invited
        if not usable:
            logger.warning("not sending: the Cognito account is unusable (status=%s, enabled=%s, email_verified=%s)",
                           account.status, account.enabled, account.email_verified)

        # 3. Record the invitation (this commits). An unusable account's sub is
        #    never passed on; if the person isn't in our database yet, nothing
        #    is recorded and the answer is the same.
        try:
            with ctx.conn.cursor() as cur:
                cur.execute(
                    "SELECT out_invitation_id, out_email, out_created FROM invite_user(%s, %s, %s, %s, %s)",
                    (org_id, body.email, body.role, body.display_name, account.sub if usable else None),
                )
                _, _, created = cur.fetchone()
            ctx.conn.commit()
        except psycopg.Error as exc:
            _rollback(ctx.conn)
            if not usable and exc.sqlstate == "P0A01":
                return InviteResponse()
            raise _db_error(exc, "invite user")
    except BaseException:
        _rollback(ctx.conn)  # releases the locks whatever went wrong; a no-op after a commit
        raise

    # 4. Send the invitation email, only now that the invitation is recorded.
    if usable and created and account.status == "FORCE_CHANGE_PASSWORD":
        try:
            cognito.resend_invitation(account.username)
        except (CognitoUnavailable, CognitoRefused):
            logger.error("invitation recorded but the email could not be sent")
    return InviteResponse()


@router.get("/orgs/{org_id}/invitations", response_model=list[OrgInvitation])
def list_org_invitations(org_id: UUID, ctx: AuthContext = Depends(get_auth_context)) -> list[OrgInvitation]:
    """Open invitations for the org. Org admins and point people only."""

    def work(cur):
        _require_member(cur, ctx, org_id, ["org_admin", "point_person"])
        cur.execute(
            "SELECT i.invitation_id, u.email, i.role, s.email, i.invited_at, i.expires_at, "
            "NOT app_is_member(i.org_id, i.invited_by, CASE WHEN i.role = 'org_admin' THEN ARRAY['org_admin'] "
            "ELSE ARRAY['org_admin', 'point_person'] END) "
            "FROM org_invitations i JOIN users u ON u.user_id = i.invited_user_id JOIN users s ON s.user_id = i.invited_by "
            "WHERE i.org_id = %s AND i.accepted_at IS NULL AND i.declined_at IS NULL AND i.revoked_at IS NULL "
            "AND i.expires_at > now() ORDER BY i.invited_at DESC, i.invitation_id",
            (org_id,),
        )
        return [OrgInvitation(**dict(zip(ORG_INVITATION_COLUMNS, row))) for row in cur.fetchall()]

    return _run(ctx, "list org invitations", work)


@router.delete("/orgs/{org_id}/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_invitation(org_id: UUID, invitation_id: UUID, ctx: AuthContext = Depends(get_auth_context)) -> Response:
    def work(cur):
        cur.execute("SELECT 1 FROM org_invitations WHERE invitation_id = %s AND org_id = %s", (invitation_id, org_id))
        if cur.fetchone() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such invitation.")
        cur.execute("SELECT revoke_invitation(%s)", (invitation_id,))

    _run(ctx, "revoke invitation", work)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me/invitations", response_model=list[MyInvitation])
def my_invitations(ctx: AuthContext = Depends(get_auth_context)) -> list[MyInvitation]:
    """Invitations waiting for the signed-in person."""

    def work(cur):
        cur.execute(
            "SELECT i.invitation_id, o.legal_name, i.role, COALESCE(s.display_name, s.email), i.expires_at "
            "FROM org_invitations i JOIN organizations o ON o.org_id = i.org_id JOIN users s ON s.user_id = i.invited_by "
            "WHERE i.invited_user_id = %s AND o.status = 'active' AND i.accepted_at IS NULL AND i.declined_at IS NULL "
            "AND i.revoked_at IS NULL AND i.expires_at > now() ORDER BY i.invited_at DESC, i.invitation_id",
            (ctx.principal.user_id,),
        )
        return [MyInvitation(**dict(zip(INVITATION_COLUMNS, row))) for row in cur.fetchall()]

    return _run(ctx, "list my invitations", work)


@router.post("/invitations/{invitation_id}/accept", status_code=status.HTTP_204_NO_CONTENT)
def accept_invitation(invitation_id: UUID, ctx: AuthContext = Depends(get_auth_context)) -> Response:
    """Accepting is the only way an invitation becomes a role. Anything that
    stops it (unknown, someone else's, used, expired, no longer allowed) is the
    same 404."""
    _run(ctx, "accept invitation", lambda cur: cur.execute("SELECT accept_invitation(%s)", (invitation_id,)))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/invitations/{invitation_id}/decline", status_code=status.HTTP_204_NO_CONTENT)
def decline_invitation(invitation_id: UUID, ctx: AuthContext = Depends(get_auth_context)) -> Response:
    _run(ctx, "decline invitation", lambda cur: cur.execute("SELECT decline_invitation(%s)", (invitation_id,)))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
