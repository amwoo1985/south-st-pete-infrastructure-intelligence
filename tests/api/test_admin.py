"""Organization / membership / support-grant routes (app/api/admin.py), over
real HTTP, real signed tokens, and a real ``app_runtime`` database login, so
the database's own rules (migration 0005) are what answer. Each denial
asserts its specific reason so a test can't pass on an unrelated error.
Setup and cleanup use the privileged role with a named system job."""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app.db import migrate
from app.db.connection import get_migration_connection
from tests.auth.conftest import cognito_env, make_token, other_key, signing_key  # noqa: F401
from tests.conftest import as_system_job


@pytest.fixture(scope="module")
def runtime_password():
    password = secrets.token_urlsafe(24)
    conn = get_migration_connection()
    try:
        migrate.ensure_runtime_role(conn, password)
    finally:
        conn.close()
    return password


@pytest.fixture
def world(runtime_password, cognito_env, make_token, monkeypatch):  # noqa: F811
    """People, a way to call the API as each of them, and cleanup."""
    tag = uuid.uuid4().hex[:8]
    admin = as_system_job(get_migration_connection(), "admin route test setup")
    people: dict[str, dict] = {}
    with admin.cursor() as cur:
        for name, platform_admin in (("pa", True), ("ed", False), ("staff", False), ("outsider", False)):
            sub = f"sub-{name}-{tag}"
            cur.execute(
                "INSERT INTO users (cognito_sub, email, is_platform_admin) VALUES (%s, %s, %s) RETURNING user_id",
                (sub, f"{name}-{tag}@example.org", platform_admin),
            )
            people[name] = {"id": cur.fetchone()[0], "sub": sub, "email": f"{name}-{tag}@example.org"}
    admin.commit()

    opened = []

    def runtime_connection():
        conn = psycopg.connect(
            host=os.environ.get("LOCAL_DB_HOST", "127.0.0.1"),
            port=os.environ.get("LOCAL_DB_PORT", "5432"),
            user=migrate.RUNTIME_ROLE,
            password=runtime_password,
            dbname=os.environ["LOCAL_DB_NAME"],
        )
        opened.append(conn)
        return conn

    # The API under test connects as the limited runtime role, like a deploy.
    monkeypatch.setattr("app.auth.context.get_connection", runtime_connection)

    def headers(name):
        return {"Authorization": "Bearer " + make_token(sub=people[name]["sub"])}

    yield people, headers, admin

    for conn in opened:
        conn.close()
    ids = [p["id"] for p in people.values()]
    with admin.cursor() as cur:
        cur.execute("SELECT org_id FROM organizations WHERE created_by = ANY(%s)", (ids,))
        orgs = [r[0] for r in cur.fetchall()]
        cur.execute(
            "DELETE FROM audit_log WHERE org_id = ANY(%s) OR actor_user_id = ANY(%s) OR object_id = ANY(%s)",
            (orgs, ids, [str(i) for i in ids]),
        )
        cur.execute("DELETE FROM support_access_grants WHERE org_id = ANY(%s)", (orgs,))
        cur.execute("DELETE FROM org_memberships WHERE org_id = ANY(%s)", (orgs,))
        cur.execute("DELETE FROM organizations WHERE org_id = ANY(%s)", (orgs,))
        cur.execute("DELETE FROM users WHERE user_id = ANY(%s)", (ids,))
    admin.commit()
    admin.close()


def _make_org(client, headers, name="Test Org"):
    resp = client.post("/orgs", json={"legal_name": f"{name} {uuid.uuid4().hex[:6]}"}, headers=headers("pa"))
    assert resp.status_code == 201, resp.text
    return resp.json()["org_id"]


def _add_member(world, org, name, role):
    """Seats someone the way the bootstrap command does (the privileged,
    reasoned path); the invitation endpoints arrive in a later layer."""
    people, _, admin = world
    with admin.cursor() as cur:
        cur.execute("SELECT set_config('app.user_id', %s, false)", (str(people["pa"]["id"]),))
        cur.execute("INSERT INTO org_memberships (org_id, user_id, role) VALUES (%s, %s, %s) RETURNING membership_id",
                    (org, people[name]["id"], role))
        membership = cur.fetchone()[0]
        cur.execute("SELECT set_config('app.user_id', '', false)")
    admin.commit()
    return str(membership)


def _org_with_admin(client, world):
    org = _make_org(client, world[1])
    _add_member(world, org, "ed", "org_admin")
    return org


def _expires(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


# --- organizations ----------------------------------------------------------


def test_only_a_platform_admin_creates_an_org(client, world):
    people, headers, admin = world
    resp = client.post("/orgs", json={"legal_name": "Nope"}, headers=headers("ed"))
    assert resp.status_code == 403
    assert "only a platform admin can create" in resp.json()["detail"]

    org = _make_org(client, headers)
    with admin.cursor() as cur:
        cur.execute("SELECT created_by, status FROM organizations WHERE org_id = %s", (org,))
        created_by, org_status = cur.fetchone()
        cur.execute("SELECT actor_user_id FROM audit_log WHERE action = 'org.create' AND object_id = %s", (org,))
        actor = cur.fetchone()[0]
    assert created_by == people["pa"]["id"] == actor
    assert org_status == "active"


def test_blank_org_name_is_422(client, world):
    _, headers, _ = world
    assert client.post("/orgs", json={"legal_name": "   "}, headers=headers("pa")).status_code == 422


def test_listing_orgs_shows_own_orgs_to_members_and_all_to_platform_admins(client, world):
    people, headers, _ = world
    org = _org_with_admin(client, world)
    mine = [o["org_id"] for o in client.get("/orgs", headers=headers("ed")).json()]
    assert mine == [org]
    assert client.get("/orgs", headers=headers("outsider")).json() == []
    assert org in [o["org_id"] for o in client.get("/orgs", headers=headers("pa")).json()]


# --- memberships ------------------------------------------------------------


def test_org_admin_lists_and_revokes_members(client, world):
    people, headers, admin = world
    org = _org_with_admin(client, world)
    membership_id = _add_member(world, org, "staff", "contributor")

    members = client.get(f"/orgs/{org}/memberships", headers=headers("ed")).json()
    assert {(m["email"], m["role"]) for m in members} == {
        (people["ed"]["email"], "org_admin"), (people["staff"]["email"], "contributor")}

    assert client.delete(f"/orgs/{org}/memberships/{membership_id}", headers=headers("ed")).status_code == 204
    remaining = client.get(f"/orgs/{org}/memberships", headers=headers("ed")).json()
    assert [m["email"] for m in remaining] == [people["ed"]["email"]]
    assert client.delete(f"/orgs/{org}/memberships/{uuid.uuid4()}", headers=headers("ed")).status_code == 404


def test_member_lists_are_for_admins_and_point_people_only(client, world):
    people, headers, _ = world
    org = _org_with_admin(client, world)
    _add_member(world, org, "staff", "contributor")
    assert client.get(f"/orgs/{org}/memberships", headers=headers("staff")).status_code == 403
    assert client.get(f"/orgs/{org}/memberships", headers=headers("outsider")).status_code == 403
    assert client.get(f"/orgs/{org}/memberships", headers=headers("pa")).status_code == 403
    # An org that doesn't exist answers the same as one you can't see.
    unknown = client.get(f"/orgs/{uuid.uuid4()}/memberships", headers=headers("ed"))
    assert unknown.status_code == 403 and unknown.json() == client.get(
        f"/orgs/{org}/memberships", headers=headers("outsider")).json()


def test_last_active_org_admin_cannot_leave(client, world):
    people, headers, _ = world
    org = _org_with_admin(client, world)
    mine = client.get(f"/orgs/{org}/memberships", headers=headers("ed")).json()[0]["membership_id"]
    resp = client.delete(f"/orgs/{org}/memberships/{mine}", headers=headers("ed"))
    assert resp.status_code == 409
    assert "at least one active admin" in resp.json()["detail"]


# --- support access ---------------------------------------------------------


def test_org_admin_grants_lists_and_revokes_support_access(client, world):
    people, headers, admin = world
    org = _org_with_admin(client, world)
    body = {"support_user_email": people["pa"]["email"], "reason": "Help with the import", "expires_at": _expires(3)}
    created = client.post(f"/orgs/{org}/support-grants", json=body, headers=headers("ed"))
    assert created.status_code == 201, created.text
    grant_id = created.json()["id"]

    with admin.cursor() as cur:
        cur.execute("SELECT consented_by FROM support_access_grants WHERE grant_id = %s", (grant_id,))
        assert cur.fetchone()[0] == people["ed"]["id"]

    listed = client.get(f"/orgs/{org}/support-grants", headers=headers("ed")).json()
    assert [(g["email"], g["revoked_at"]) for g in listed] == [(people["pa"]["email"], None)]

    assert client.delete(f"/orgs/{org}/support-grants/{grant_id}", headers=headers("ed")).status_code == 204
    after = client.get(f"/orgs/{org}/support-grants", headers=headers("ed")).json()
    assert after[0]["revoked_at"] is not None
    assert client.delete(f"/orgs/{org}/support-grants/{uuid.uuid4()}", headers=headers("ed")).status_code == 404


def test_support_access_rules_are_the_databases(client, world):
    people, headers, _ = world
    org = _org_with_admin(client, world)
    _add_member(world, org, "staff", "contributor")
    base = {"support_user_email": people["pa"]["email"], "reason": "Help"}
    # Only the customer's org admin consents; the vendor can't consent for them.
    assert client.post(f"/orgs/{org}/support-grants", json={**base, "expires_at": _expires(3)},
                       headers=headers("staff")).status_code == 403
    assert client.post(f"/orgs/{org}/support-grants", json={**base, "expires_at": _expires(3)},
                       headers=headers("pa")).status_code == 403
    # At most 14 days, and in the future (checked on the way in).
    too_long = client.post(f"/orgs/{org}/support-grants", json={**base, "expires_at": _expires(30)},
                           headers=headers("ed"))
    assert too_long.status_code == 422
    assert client.post(f"/orgs/{org}/support-grants", json={**base, "expires_at": _expires(-1)},
                       headers=headers("ed")).status_code == 422
    # Only a platform admin can be given support access.
    to_member = client.post(f"/orgs/{org}/support-grants",
                            json={"support_user_email": people["staff"]["email"], "reason": "Help",
                                  "expires_at": _expires(3)}, headers=headers("ed"))
    assert to_member.status_code == 409
    assert "platform admin" in to_member.json()["detail"]


def test_support_grants_list_is_for_org_admins_only(client, world):
    people, headers, _ = world
    org = _org_with_admin(client, world)
    assert client.get(f"/orgs/{org}/support-grants", headers=headers("outsider")).status_code == 403
    assert client.get(f"/orgs/{org}/support-grants", headers=headers("pa")).status_code == 403


def test_naive_expiry_time_is_422(client, world):
    people, headers, _ = world
    org = _org_with_admin(client, world)
    body = {"support_user_email": people["pa"]["email"], "reason": "Help", "expires_at": "2030-01-01T00:00:00"}
    assert client.post(f"/orgs/{org}/support-grants", json=body, headers=headers("ed")).status_code == 422


# --- no oracles, bad input, other rules --------------------------------------


def test_strangers_learn_nothing_from_the_write_routes(client, world):
    people, headers, _ = world
    org = _org_with_admin(client, world)
    fake_org = str(uuid.uuid4())
    answers = set()
    for target_org in (org, fake_org):
        for email in (people["staff"]["email"], people["pa"]["email"], "nobody-here@example.org"):
            grant = client.post(f"/orgs/{target_org}/support-grants",
                                json={"support_user_email": email, "reason": "x", "expires_at": _expires(3)},
                                headers=headers("outsider"))
            assert grant.status_code == 403
            answers.add(grant.json()["detail"])
    assert len(answers) == 1


def test_revoking_twice_is_refused(client, world):
    people, headers, _ = world
    org = _org_with_admin(client, world)
    mid = _add_member(world, org, "staff", "contributor")
    assert client.delete(f"/orgs/{org}/memberships/{mid}", headers=headers("ed")).status_code == 204
    again = client.delete(f"/orgs/{org}/memberships/{mid}", headers=headers("ed"))
    assert again.status_code == 409


def test_nul_bytes_are_422_not_500(client, world):
    people, headers, _ = world
    assert client.post("/orgs", json={"legal_name": "Bad\u0000Name"}, headers=headers("pa")).status_code == 422
    org = _org_with_admin(client, world)
    body = {"support_user_email": people["pa"]["email"], "reason": "x\u0000y", "expires_at": _expires(3)}
    assert client.post(f"/orgs/{org}/support-grants", json=body, headers=headers("ed")).status_code == 422


def test_database_error_text_is_never_shown(client, world, monkeypatch):
    from app.api import admin as admin_module

    people, headers, _ = world
    from psycopg import errors as pgerr

    def boom(cur):
        raise pgerr.InsufficientPrivilege('permission denied for table users_secret')

    org = _org_with_admin(client, world)
    monkeypatch.setattr(admin_module, "_active_user_id", lambda cur, email: (_ for _ in ()).throw(boom(cur)))
    body = {"support_user_email": people["pa"]["email"], "reason": "x", "expires_at": _expires(3)}
    resp = client.post(f"/orgs/{org}/support-grants", json=body, headers=headers("ed"))
    assert resp.status_code == 403
    assert "users_secret" not in resp.text


# --- sign-in is required ----------------------------------------------------


@pytest.mark.parametrize("method,path", [
    ("GET", "/orgs"), ("POST", "/orgs"),
    ("GET", f"/orgs/{uuid.uuid4()}/memberships"),
    ("DELETE", f"/orgs/{uuid.uuid4()}/memberships/{uuid.uuid4()}"),
    ("GET", f"/orgs/{uuid.uuid4()}/support-grants"), ("POST", f"/orgs/{uuid.uuid4()}/support-grants"),
    ("DELETE", f"/orgs/{uuid.uuid4()}/support-grants/{uuid.uuid4()}"),
])
def test_every_admin_route_needs_sign_in(client, cognito_env, method, path):  # noqa: F811
    assert client.request(method, path).status_code == 401
