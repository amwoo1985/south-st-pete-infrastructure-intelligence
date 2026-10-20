"""Test fixtures for app/auth: a locally generated RSA key pair standing in
for Cognito's signing key, and a factory for real RS256-signed tokens.
No network access; the JWKS lookup is replaced with the local key."""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import tokens

REGION = "us-east-1"
POOL = "us-east-1_TESTPOOL"
CLIENT = "test-client-id"
KID = "test-kid-1"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"


@pytest.fixture(scope="session")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def other_key():
    """A different key: tokens signed with it must be rejected."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def cognito_env(monkeypatch, signing_key):
    monkeypatch.setenv(tokens.REGION_ENV, REGION)
    monkeypatch.setenv(tokens.POOL_ENV, POOL)
    monkeypatch.setenv(tokens.CLIENT_ENV, CLIENT)

    def fake_signing_key(token, config):
        kid = jwt.get_unverified_header(token).get("kid")
        if kid != KID:
            # Same contract as the real _signing_key: unknown kid -> TokenError.
            raise tokens.TokenError(f'signing key not found: no key matches "{kid}"')
        return signing_key.public_key()

    monkeypatch.setattr(tokens, "_signing_key", fake_signing_key)


@pytest.fixture
def make_token(signing_key):
    def _make(*, key=None, kid=KID, alg="RS256", headers=None, **overrides):
        now = int(time.time())
        claims = {
            "sub": "sub-amber",
            "iss": ISSUER,
            "client_id": CLIENT,
            "token_use": "access",
            "scope": "openid email",
            "iat": now,
            "exp": now + 3600,
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        hdrs = {"kid": kid} if kid is not None else {}
        hdrs.update(headers or {})
        return jwt.encode(claims, key or signing_key, algorithm=alg, headers=hdrs)

    return _make
