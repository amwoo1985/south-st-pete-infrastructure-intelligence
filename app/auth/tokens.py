"""Cognito access-token verification (DECISIONS #164).

Every authenticated request carries a Cognito *access* token as
``Authorization: Bearer <token>``. This module decides whether that token is
genuine and current, and returns its claims. It says nothing about what the
person may do; that's the app database's job (app/auth/principal.py and the
authorization layer).

Checks, per AWS's access-token documentation (verified, DECISIONS #158 plan
review): RS256 signature by a key from the user pool's published JWKS
(matched by ``kid``), ``iss`` equal to the user pool's issuer URL,
``token_use == "access"``, ``client_id`` equal to our app client, and an
unexpired ``exp``. ``aud`` is deliberately NOT checked: Cognito access
tokens carry ``client_id`` instead and have no ``aud`` claim. ID tokens are
rejected even though they're validly signed, because they aren't meant to
authorize API calls.

Fails closed: if the Cognito settings aren't configured, every token is
rejected rather than the check being skipped. If Cognito's key set can't be
fetched or read, that's reported as "auth unavailable" (a 503 upstream), not
as a bad token, so an outage doesn't look like everyone signing in wrong.

Limits, stated plainly: verification is local, so a token stays valid until
its ``exp`` (60 minutes here) even after the user signs out or the token is
revoked in Cognito. The kill switch for a person is ``users.disabled_at``
(app/auth/principal.py), checked on every request. ``exp``/``iat`` allow 30
seconds of clock drift between this server and Cognito. A rotated Cognito
key is picked up on the first request that carries its new ``kid`` (at most
one forced refetch per 30 seconds).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

import jwt

REGION_ENV = "COGNITO_REGION"
POOL_ENV = "COGNITO_USER_POOL_ID"
CLIENT_ENV = "COGNITO_APP_CLIENT_ID"

# Only the algorithm Cognito signs with. Never accept "none" or an HMAC
# algorithm: with HS256, a public RSA key could be misused as an HMAC secret.
_ALGORITHMS = ["RS256"]


class TokenError(Exception):
    """The token is missing, malformed, forged, expired or not ours. The
    message is safe to log but is not shown to the caller in detail."""


class AuthNotConfiguredError(Exception):
    """COGNITO_* settings are missing; authentication can't be performed."""


class AuthUnavailableError(Exception):
    """Cognito's signing keys couldn't be fetched or read (network failure,
    timeout, malformed key set). Not the caller's fault; retry later."""


@dataclass(frozen=True)
class CognitoConfig:
    region: str
    user_pool_id: str
    app_client_id: str

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"


def load_config() -> CognitoConfig:
    values = {name: (os.environ.get(name) or "").strip() for name in (REGION_ENV, POOL_ENV, CLIENT_ENV)}
    missing = [name for name, v in values.items() if not v or v.lower().startswith("changeme")]
    if missing:
        raise AuthNotConfiguredError("Cognito settings missing: " + ", ".join(missing))
    return CognitoConfig(values[REGION_ENV], values[POOL_ENV], values[CLIENT_ENV])


@lru_cache(maxsize=4)
def _jwks_client(jwks_url: str) -> jwt.PyJWKClient:
    # Caches the key set (refetched after `lifespan` seconds, or on an
    # unknown kid, which is how Cognito key rotation is picked up). A short
    # timeout keeps a slow JWKS fetch from hanging requests.
    return jwt.PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=3600, timeout=5, cooldown_duration=30)


def _signing_key(token: str, config: CognitoConfig):
    """The public key that should have signed this token. Raises TokenError
    when the token's kid isn't one of Cognito's keys, AuthUnavailableError
    when the key set itself can't be obtained. Separate function so tests
    can supply their own key set without network access."""
    try:
        return _jwks_client(config.jwks_url).get_signing_key_from_jwt(token).key
    except jwt.PyJWKClientConnectionError as exc:  # subclass of PyJWKClientError: check first
        raise AuthUnavailableError(f"could not fetch Cognito signing keys: {exc}") from exc
    except (jwt.exceptions.PyJWKSetError, ValueError) as exc:
        raise AuthUnavailableError(f"unusable Cognito key set: {exc}") from exc
    except jwt.PyJWKClientError as exc:
        raise TokenError(f"signing key not found: {exc}") from exc


def verify_access_token(token: str, config: CognitoConfig | None = None) -> dict:
    """Returns the verified claims of a Cognito access token. Raises
    TokenError for a bad token, AuthUnavailableError when Cognito's keys
    can't be obtained, AuthNotConfiguredError when config is missing."""
    config = config or load_config()
    if not token:
        raise TokenError("empty token")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise TokenError(f"malformed token: {exc}") from exc
    if header.get("alg") not in _ALGORITHMS:
        raise TokenError(f"unexpected algorithm {header.get('alg')!r}")
    if not header.get("kid"):
        raise TokenError("token has no key id")
    key = _signing_key(token, config)
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=_ALGORITHMS,
            issuer=config.issuer,
            leeway=30,
            options={
                "require": ["exp", "iat", "iss", "sub", "token_use", "client_id"],
                "verify_aud": False,  # access tokens have client_id, not aud
            },
        )
    except jwt.PyJWTError as exc:
        raise TokenError(f"invalid token: {exc}") from exc
    if claims.get("token_use") != "access":
        raise TokenError(f"not an access token (token_use={claims.get('token_use')!r})")
    if claims.get("client_id") != config.app_client_id:
        raise TokenError("token was issued to a different app client")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise TokenError("token has no usable subject")
    return claims
