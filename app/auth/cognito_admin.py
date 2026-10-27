"""The few Cognito user-pool operations the API needs to invite people
(DECISIONS #173): find an account by email, create one WITHOUT sending
anything, and send the invitation email later.

Why it is shaped this way (AWS docs checked 2026-10-21, DECISIONS #173):
- Lookup is ``ListUsers`` with an ``email`` filter. ``AdminGetUser`` is not
  documented to accept an email address, so it isn't used. ``ListUsers`` is
  eventually consistent, so a just-created account can be missed; creation
  failing with "already exists" is handled by looking once more.
- Creation uses ``MessageAction=SUPPRESS``: Cognito emails a temporary
  password on creation, and that must not go out before the database has
  accepted the invitation. After the database commits, ``resend_invitation``
  sends it (``MessageAction=RESEND``).
- The pool signs people in by email, so Cognito generates the account's
  ``Username``; it is only ever passed back to Cognito for the resend. The
  identity we store and trust is the ``sub``, read from Cognito's own reply,
  never from a request.

The API's task role is allowed exactly ``cognito-idp:AdminCreateUser`` and
``cognito-idp:ListUsers`` on this one user pool (infra/api-task-role-policy.json).

Errors are reduced to two kinds: ``CognitoUnavailable`` (throttling, outage,
missing permission, any Cognito refusal, anything we can't act on; the code is
logged, never shown to callers) and ``CognitoRefused`` (we declined to send the
address to Cognito at all because it isn't a plain safe email).

Known limits: ``ListUsers``'s email filter may be case-sensitive (AWS's page
doesn't say). The pool only holds accounts we create with a lowercased email, so
this is expected to hold; an account someone created by hand with mixed case would
not be found, and inviting that address answers 502.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, replace
from functools import lru_cache

logger = logging.getLogger("auth.cognito_admin")

REGION_ENV = "COGNITO_REGION"
POOL_ENV = "COGNITO_USER_POOL_ID"

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
# An email is placed inside a ListUsers filter string, so only the characters
# the database also accepts are allowed (no quotes, backslashes or spaces).
_SAFE_EMAIL_RE = re.compile(r"^[a-z0-9.!#$%&*+/=?^_`{|}~'-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$")


class CognitoUnavailable(Exception):
    """Cognito couldn't be used right now (or at all): throttled, down, or our
    role lacks permission. Not the caller's fault."""


class CognitoRefused(Exception):
    """Cognito rejected this address."""


@dataclass(frozen=True)
class CognitoAccount:
    sub: str
    username: str
    enabled: bool
    status: str
    email_verified: bool = False

    @property
    def can_be_invited(self) -> bool:
        """Signed in already, or holding a not-yet-used temporary password, with
        a verified email (so the sub really belongs to whoever owns that
        mailbox). Disabled, reset-required, compromised and unverified accounts
        are left alone."""
        return self.enabled and self.email_verified and self.status in ("CONFIRMED", "FORCE_CHANGE_PASSWORD")


class CognitoAdmin:
    def __init__(self, client, user_pool_id: str):
        self._client = client
        self._pool = user_pool_id

    @staticmethod
    def _to_account(user: dict) -> CognitoAccount:
        try:
            return CognitoAdmin._parse_account(user)
        except (KeyError, TypeError, AttributeError) as exc:
            raise CognitoUnavailable(f"unexpected reply shape: {type(exc).__name__}")

    @staticmethod
    def _parse_account(user: dict) -> CognitoAccount:
        attrs = {a["Name"]: a["Value"] for a in user.get("Attributes", user.get("UserAttributes", []))}
        sub = (attrs.get("sub") or "").strip().lower()
        if not _UUID_RE.match(sub):
            raise CognitoUnavailable("Cognito returned an account without a usable sub")
        return CognitoAccount(
            sub=sub,
            username=user["Username"],
            enabled=bool(user.get("Enabled", True)),
            status=user.get("UserStatus", "UNKNOWN"),
            email_verified=str(attrs.get("email_verified", "")).lower() == "true",
        )

    def _call(self, what: str, fn, **kwargs):
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return fn(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("UsernameExistsException", "AliasExistsException"):
                raise
            # Everything else (throttling, outage, missing permission, a rejected
            # parameter that points at pool configuration) is our side's problem.
            logger.error("cognito %s failed: %s (%s)", what, code or "ClientError", exc.response.get("Error", {}).get("Message", ""))
            raise CognitoUnavailable(code or "ClientError")
        except BotoCoreError as exc:
            logger.error("cognito %s unreachable: %s", what, exc)
            raise CognitoUnavailable(type(exc).__name__)

    def find_by_email(self, email: str) -> CognitoAccount | None:
        if not _SAFE_EMAIL_RE.match(email):
            raise CognitoRefused("unsafe email")
        reply = self._call(
            "ListUsers", self._client.list_users,
            UserPoolId=self._pool, Filter=f'email = "{email}"', Limit=2,
        )
        users = reply.get("Users", [])
        if not users:
            return None
        if len(users) > 1:
            logger.error("cognito has more than one account for the same email")
            raise CognitoUnavailable("ambiguous account")
        return self._to_account(users[0])

    def create_without_message(self, email: str) -> CognitoAccount:
        """Creates the account and sends nothing. If it already exists (a
        concurrent invite, or ListUsers lagging), looks it up once more."""
        from botocore.exceptions import ClientError

        try:
            reply = self._call(
                "AdminCreateUser", self._client.admin_create_user,
                UserPoolId=self._pool, Username=email, MessageAction="SUPPRESS",
                UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
                DesiredDeliveryMediums=["EMAIL"],
            )
            # We set email_verified ourselves; the reply may not list it.
            return replace(self._to_account(reply.get("User") or {}), email_verified=True)
        except ClientError:  # UsernameExists / AliasExists
            for attempt in range(2):
                found = self.find_by_email(email)
                if found is not None:
                    return found
                if attempt == 0:
                    time.sleep(1.0)  # ListUsers is eventually consistent
            raise CognitoUnavailable("account exists but could not be found")

    def resend_invitation(self, username: str) -> None:
        """Sends the invitation email (new temporary password) for an account
        that hasn't signed in yet."""
        self._call(
            "AdminCreateUser(RESEND)", self._client.admin_create_user,
            UserPoolId=self._pool, Username=username, MessageAction="RESEND",
            DesiredDeliveryMediums=["EMAIL"],
        )


@lru_cache(maxsize=1)
def _build_admin(region: str, pool_id: str) -> CognitoAdmin:
    import boto3
    from botocore.config import Config

    # A stuck Cognito call must not hold a worker thread and a database
    # transaction for minutes.
    config = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2, "mode": "standard"})
    return CognitoAdmin(boto3.client("cognito-idp", region_name=region, config=config), pool_id)


def get_cognito_admin() -> CognitoAdmin:
    """FastAPI dependency. Fails closed when the pool isn't configured."""
    region = (os.environ.get(REGION_ENV) or "").strip()
    pool = (os.environ.get(POOL_ENV) or "").strip()
    if not region or not pool or pool.lower().startswith("changeme"):
        raise CognitoUnavailable("COGNITO_REGION / COGNITO_USER_POOL_ID are not configured")
    try:
        return _build_admin(region, pool)
    except Exception as exc:  # missing boto3, bad region name, ...
        logger.error("could not build the Cognito client: %s", exc)
        raise CognitoUnavailable("could not build the Cognito client")
