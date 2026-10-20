"""app/auth/tokens.py: accept only genuine, current Cognito access tokens
issued to our app client."""

from __future__ import annotations

import base64
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from app.auth import tokens
from app.auth.tokens import AuthNotConfiguredError, AuthUnavailableError, TokenError, verify_access_token

from .conftest import CLIENT


def test_valid_access_token_returns_claims(cognito_env, make_token):
    claims = verify_access_token(make_token())
    assert claims["sub"] == "sub-amber"
    assert claims["client_id"] == CLIENT


def test_expired_token_rejected(cognito_env, make_token):
    now = int(time.time())
    with pytest.raises(TokenError):
        verify_access_token(make_token(iat=now - 7200, exp=now - 60))


def test_wrong_issuer_rejected(cognito_env, make_token):
    with pytest.raises(TokenError):
        verify_access_token(make_token(iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_OTHERPOOL"))


def test_id_token_rejected_even_though_validly_signed(cognito_env, make_token):
    with pytest.raises(TokenError, match="not an access token"):
        verify_access_token(make_token(token_use="id", aud=CLIENT))


def test_token_for_another_app_client_rejected(cognito_env, make_token):
    with pytest.raises(TokenError, match="different app client"):
        verify_access_token(make_token(client_id="someone-elses-client"))


def test_token_signed_by_another_key_rejected(cognito_env, make_token, other_key):
    with pytest.raises(TokenError):
        verify_access_token(make_token(key=other_key))


def test_unknown_kid_rejected(cognito_env, make_token):
    with pytest.raises(TokenError, match="signing key not found"):
        verify_access_token(make_token(kid="not-a-cognito-kid"))


def test_missing_kid_rejected(cognito_env, make_token):
    with pytest.raises(TokenError, match="no key id"):
        verify_access_token(make_token(kid=None))


@pytest.mark.parametrize("claim", ["exp", "iat", "sub", "token_use", "client_id"])
def test_missing_required_claim_rejected(cognito_env, make_token, claim):
    with pytest.raises(TokenError):
        verify_access_token(make_token(**{claim: None}))


def test_alg_none_rejected(cognito_env):
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    forged = b64({"alg": "none", "kid": "test-kid-1"}) + "." + b64({"sub": "sub-amber"}) + "."
    with pytest.raises(TokenError, match="unexpected algorithm"):
        verify_access_token(forged)


def test_hs256_with_public_key_as_secret_rejected(cognito_env, signing_key):
    """Classic algorithm-confusion forgery: sign HS256 using the (public)
    RSA key bytes as the HMAC secret."""
    import hmac
    import hashlib

    public_pem = signing_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    now = int(time.time())
    header = b64(json.dumps({"alg": "HS256", "kid": "test-kid-1"}).encode())
    payload = b64(json.dumps({"sub": "sub-amber", "token_use": "access", "client_id": CLIENT, "iat": now, "exp": now + 600}).encode())
    signature = b64(hmac.new(public_pem, f"{header}.{payload}".encode(), hashlib.sha256).digest())
    with pytest.raises(TokenError, match="unexpected algorithm"):
        verify_access_token(f"{header}.{payload}.{signature}")


def test_tampered_payload_rejected(cognito_env, make_token):
    token = make_token()
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["sub"] = "sub-someone-else"
    forged_payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    with pytest.raises(TokenError):
        verify_access_token(f"{header}.{forged_payload}.{signature}")


@pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b.c", "Bearer xyz"])
def test_garbage_rejected(cognito_env, garbage):
    with pytest.raises(TokenError):
        verify_access_token(garbage)


def test_missing_config_fails_closed(monkeypatch, make_token):
    for name in (tokens.REGION_ENV, tokens.POOL_ENV, tokens.CLIENT_ENV):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(AuthNotConfiguredError):
        verify_access_token(make_token())


def test_placeholder_config_fails_closed(monkeypatch, make_token):
    monkeypatch.setenv(tokens.REGION_ENV, "us-east-1")
    monkeypatch.setenv(tokens.POOL_ENV, "changeme")
    monkeypatch.setenv(tokens.CLIENT_ENV, "changeme")
    with pytest.raises(AuthNotConfiguredError):
        verify_access_token(make_token())


def _real_lookup_env(monkeypatch):
    monkeypatch.setenv(tokens.REGION_ENV, "us-east-1")
    monkeypatch.setenv(tokens.POOL_ENV, "us-east-1_TESTPOOL")
    monkeypatch.setenv(tokens.CLIENT_ENV, CLIENT)


class _FailingClient:
    def __init__(self, exc):
        self.exc = exc

    def get_signing_key_from_jwt(self, token):
        raise self.exc


@pytest.mark.parametrize(
    "exc",
    [
        jwt.PyJWKClientConnectionError("connection refused"),
        jwt.exceptions.PyJWKSetError("no usable keys"),
        ValueError("not json"),
    ],
)
def test_unreachable_or_broken_key_set_is_unavailable_not_bad_token(monkeypatch, make_token, exc):
    _real_lookup_env(monkeypatch)
    monkeypatch.setattr(tokens, "_jwks_client", lambda url: _FailingClient(exc))
    with pytest.raises(AuthUnavailableError):
        verify_access_token(make_token())


def test_unknown_kid_from_real_client_is_bad_token(monkeypatch, make_token):
    _real_lookup_env(monkeypatch)
    monkeypatch.setattr(tokens, "_jwks_client", lambda url: _FailingClient(jwt.PyJWKClientError("no matching key")))
    with pytest.raises(TokenError, match="signing key not found"):
        verify_access_token(make_token())


def test_decode_enforces_rs256_even_without_the_header_precheck(cognito_env, signing_key, monkeypatch):
    """Second layer: if the header pre-check were removed, jwt.decode's
    algorithms list must still refuse a non-RS256 token."""
    monkeypatch.setattr(tokens, "_ALGORITHMS", ["RS256", "HS256"])  # let the pre-check pass HS256
    now = int(time.time())
    forged = jwt.encode(
        {"sub": "sub-amber", "iss": "x", "token_use": "access", "client_id": CLIENT, "iat": now, "exp": now + 600},
        "some-secret-long-enough-for-hmac-32b!",
        algorithm="HS256",
        headers={"kid": "test-kid-1"},
    )
    monkeypatch.setattr(tokens, "_ALGORITHMS", ["RS256"])
    with pytest.raises(TokenError):
        verify_access_token(forged)


def test_small_clock_skew_tolerated(cognito_env, make_token):
    now = int(time.time())
    verify_access_token(make_token(iat=now + 10))


def test_large_future_iat_rejected(cognito_env, make_token):
    now = int(time.time())
    with pytest.raises(TokenError):
        verify_access_token(make_token(iat=now + 600))
