"""app/auth/principal.py: the per-request identity check, over a real HTTP
request and the real local Postgres `users` table."""

from __future__ import annotations

import uuid

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth.principal import Principal, get_principal
from app.db.connection import get_connection
from tests.conftest import as_system_job


@pytest.fixture
def probe_app():
    """A minimal app with one protected route, so these tests exercise the
    dependency exactly as a real route will use it."""
    app = FastAPI()

    @app.get("/whoami")
    def whoami(principal: Principal = Depends(get_principal)):
        return {"email": principal.email, "is_platform_admin": principal.is_platform_admin}

    return TestClient(app)


def _sub() -> str:
    return "sub-test-" + uuid.uuid4().hex[:12]


def test_active_user_gets_principal(cognito_env, make_token, probe_app, user_row):
    sub = _sub()
    user_row(sub)
    r = probe_app.get("/whoami", headers={"Authorization": f"Bearer {make_token(sub=sub)}"})
    assert r.status_code == 200
    assert r.json() == {"email": f"{sub}@example.org", "is_platform_admin": False}


def test_no_header_is_401_with_bearer_challenge(cognito_env, probe_app):
    r = probe_app.get("/whoami")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_non_bearer_scheme_is_401(cognito_env, make_token, probe_app):
    r = probe_app.get("/whoami", headers={"Authorization": f"Basic {make_token()}"})
    assert r.status_code == 401


def test_invalid_token_is_401(cognito_env, probe_app):
    r = probe_app.get("/whoami", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


def test_valid_token_without_account_is_403(cognito_env, make_token, probe_app):
    r = probe_app.get("/whoami", headers={"Authorization": f"Bearer {make_token(sub=_sub())}"})
    assert r.status_code == 403


def test_disabled_user_is_403_even_with_valid_token(cognito_env, make_token, probe_app, user_row):
    sub = _sub()
    user_row(sub, disabled=True)
    r = probe_app.get("/whoami", headers={"Authorization": f"Bearer {make_token(sub=sub)}"})
    assert r.status_code == 403


def test_disabling_takes_effect_on_next_request(cognito_env, make_token, probe_app, user_row):
    sub = _sub()
    user_row(sub)
    token = make_token(sub=sub)
    assert probe_app.get("/whoami", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    conn = as_system_job(get_connection())
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET disabled_at = now() WHERE cognito_sub = %s", (sub,))
        conn.commit()
    finally:
        conn.close()
    assert probe_app.get("/whoami", headers={"Authorization": f"Bearer {token}"}).status_code == 403


def test_unconfigured_auth_is_503(monkeypatch, make_token, probe_app):
    for name in ("COGNITO_REGION", "COGNITO_USER_POOL_ID", "COGNITO_APP_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    r = probe_app.get("/whoami", headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 503


def test_unreachable_cognito_keys_is_503(cognito_env, make_token, probe_app, monkeypatch):
    from app.auth import tokens

    def unavailable(token, config):
        raise tokens.AuthUnavailableError("connection refused")

    monkeypatch.setattr(tokens, "_signing_key", unavailable)
    r = probe_app.get("/whoami", headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 503


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer ", "Basic abc", "bearer not-a-token", "Bearer  junk.junk.junk"],
)
def test_requests_without_a_genuine_token_never_open_a_db_connection(cognito_env, probe_app, monkeypatch, header):
    from app.auth import principal as principal_module

    def no_db():
        raise AssertionError("opened a DB connection for an unauthenticated request")

    monkeypatch.setattr(principal_module, "get_connection", no_db)
    headers = {} if header is None else {"Authorization": header}
    assert probe_app.get("/whoami", headers=headers).status_code == 401


def test_lowercase_bearer_scheme_accepted(cognito_env, make_token, probe_app, user_row):
    sub = _sub()
    user_row(sub)
    r = probe_app.get("/whoami", headers={"Authorization": f"bearer {make_token(sub=sub)}"})
    assert r.status_code == 200


def test_database_down_after_valid_token_is_503(cognito_env, make_token, probe_app, monkeypatch):
    import psycopg

    from app.auth import principal as principal_module

    def db_down():
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(principal_module, "get_connection", db_down)
    r = probe_app.get("/whoami", headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 503
