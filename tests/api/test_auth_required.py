"""Every API route except /health needs a signed-in user (DECISIONS #169),
checked with real signed tokens and real `users` rows, not the sign-in
stand-in the other tests in this directory use (tests/api/conftest.py)."""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth.context import AuthContext, get_auth_context
from app.db.connection import get_connection
from app.rag.generation import UngroundedAnswerError

# Imported so pytest finds the token and user fixtures in this module (they
# live in tests/auth/conftest.py, which this directory can't see).
from tests.auth.conftest import cognito_env, make_token, other_key, signing_key, user_row  # noqa: F401

PROTECTED = [
    ("POST", "/query", {"json": {"question": "What are the eligibility rules?"}}),
    ("GET", "/sources/does-not-exist", {}),
    ("POST", "/documents/upload", {"files": {"file": ("x.txt", b"content", "text/plain")}}),
]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("method,path,kwargs", PROTECTED)
def test_no_token_is_401(client, cognito_env, method, path, kwargs):  # noqa: F811
    resp = client.request(method, path, **kwargs)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Sign-in required."


@pytest.mark.parametrize("method,path,kwargs", PROTECTED)
def test_retired_upload_key_opens_nothing(client, cognito_env, monkeypatch, method, path, kwargs):  # noqa: F811
    # Even with the old env var set to the very value sent, the header is ignored.
    monkeypatch.setenv("UPLOAD_API_KEY", "retired-key")
    resp = client.request(method, path, headers={"X-Upload-Api-Key": "retired-key"}, **kwargs)
    assert resp.status_code == 401


@pytest.mark.parametrize("method,path,kwargs", PROTECTED)
def test_forged_token_is_401(client, cognito_env, make_token, other_key, method, path, kwargs):  # noqa: F811
    resp = client.request(method, path, headers=_bearer(make_token(key=other_key)), **kwargs)
    assert resp.status_code == 401


@pytest.mark.parametrize("method,path,kwargs", PROTECTED)
def test_valid_token_without_account_is_403(client, cognito_env, make_token, method, path, kwargs):  # noqa: F811
    token = make_token(sub="sub-nobody-" + uuid.uuid4().hex[:10])
    resp = client.request(method, path, headers=_bearer(token), **kwargs)
    assert resp.status_code == 403


def test_unconfigured_sign_in_is_503_on_every_protected_route(client, monkeypatch):
    for name in ("COGNITO_REGION", "COGNITO_USER_POOL_ID", "COGNITO_APP_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    for method, path, kwargs in PROTECTED:
        resp = client.request(method, path, headers=_bearer("not-a-real-token"), **kwargs)
        assert resp.status_code == 503, path


def test_anonymous_callers_with_bad_bodies_still_get_401_not_validation_errors(client, cognito_env):  # noqa: F811
    # A well-formed JSON body missing its field, and an upload with no file
    # part: the sign-in check runs before body validation, so neither
    # reveals anything about the request shape to an anonymous caller.
    assert client.post("/query", json={}).status_code == 401
    assert client.post("/documents/upload").status_code == 401


def test_signed_in_user_reaches_sources(client, cognito_env, make_token, user_row):  # noqa: F811
    sub = "sub-test-" + uuid.uuid4().hex[:12]
    user_row(sub)
    resp = client.get("/sources/does-not-exist", headers=_bearer(make_token(sub=sub)))
    assert resp.status_code == 404  # past sign-in; the document just isn't there


def test_signed_in_user_reaches_query(client, cognito_env, make_token, user_row, monkeypatch):  # noqa: F811
    sub = "sub-test-" + uuid.uuid4().hex[:12]
    user_row(sub)

    def ungrounded(*args, **kwargs):
        raise UngroundedAnswerError("no valid citations survived after retry")

    monkeypatch.setattr("app.api.query.answer_query", ungrounded)
    resp = client.post("/query", json={"question": "What is the CBA?"}, headers=_bearer(make_token(sub=sub)))
    assert resp.status_code == 200
    assert resp.json()["not_in_corpus"] is True


def test_signed_in_user_can_upload(client, cognito_env, make_token, user_row, monkeypatch):  # noqa: F811
    sub = "sub-test-" + uuid.uuid4().hex[:12]
    user_row(sub)
    monkeypatch.setattr("app.api.documents._process_upload_background", MagicMock())
    content = ("auth-required upload " + uuid.uuid4().hex).encode()
    file_hash = hashlib.sha256(content).hexdigest()
    try:
        resp = client.post(
            "/documents/upload",
            files={"file": ("auth.txt", content, "text/plain")},
            headers=_bearer(make_token(sub=sub)),
        )
        assert resp.status_code == 202
        assert resp.json()["file_hash"] == file_hash
    finally:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM document_uploads WHERE file_hash = %s", (file_hash,))
            conn.commit()
        finally:
            conn.close()


def test_acting_user_survives_a_route_rollback(cognito_env, make_token, user_row):  # noqa: F811
    # The upload route rolls back on some error paths; the identity the
    # database reads (app.user_id) must still be set afterwards (#166).
    sub = "sub-test-" + uuid.uuid4().hex[:12]
    user_row(sub)
    probe = FastAPI()

    @probe.get("/probe")
    def probe_route(ctx: AuthContext = Depends(get_auth_context)):
        ctx.conn.rollback()
        with ctx.conn.cursor() as cur:
            cur.execute("SELECT current_setting('app.user_id', true)")
            return {"acting": cur.fetchone()[0], "user_id": str(ctx.principal.user_id)}

    body = TestClient(probe).get("/probe", headers=_bearer(make_token(sub=sub))).json()
    assert body["acting"] == body["user_id"]


def test_health_and_static_page_stay_open(client):
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200
