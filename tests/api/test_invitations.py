"""Invitation routes (app/api/invitations.py) over real HTTP, real signed
tokens and a real app_runtime database login, with Cognito replaced by a
recording fake. The database rules themselves are tested in
tests/db/test_invitations.py; here the point is the order of events and what
the API does and doesn't reveal."""

from __future__ import annotations

import uuid

import pytest

from app.api import invitations as invitations_module
from app.api.main import app
from app.auth.cognito_admin import CognitoAccount, CognitoUnavailable
from tests.api.test_admin import _add_member, _make_org, _org_with_admin, runtime_password, world  # noqa: F401
from tests.auth.conftest import cognito_env, make_token, other_key, signing_key  # noqa: F401


class FakeCognito:
    def __init__(self):
        self.accounts: dict[str, CognitoAccount] = {}
        self.calls: list[tuple] = []
        self.unavailable = False
        self.resend_fails = False
        self.on_resend = None

    def find_by_email(self, email):
        self.calls.append(("find", email))
        if self.unavailable:
            raise CognitoUnavailable("down")
        return self.accounts.get(email)

    def create_without_message(self, email):
        self.calls.append(("create", email))
        if self.unavailable:
            raise CognitoUnavailable("down")
        account = CognitoAccount(sub=str(uuid.uuid4()), username="gen-" + uuid.uuid4().hex, enabled=True,
                                 status="FORCE_CHANGE_PASSWORD", email_verified=True)
        self.accounts[email] = account
        return account

    def resend_invitation(self, username):
        self.calls.append(("resend", username))
        if self.on_resend:
            self.on_resend(username)
        if self.resend_fails:
            raise CognitoUnavailable("down")

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def cognito():
    fake = FakeCognito()
    app.dependency_overrides[invitations_module.cognito_provider] = lambda: (lambda: fake)
    yield fake
    app.dependency_overrides.pop(invitations_module.cognito_provider, None)


def _new_email(people, label="newbie"):
    suffix = people["ed"]["email"].split("-", 1)[1]
    return f"{label}-{uuid.uuid4().hex[:6]}-{suffix}"


def _invite(client, headers, org, email, role="contributor", who="ed", **extra):
    return client.post(f"/orgs/{org}/invitations", json={"email": email, "role": role, **extra}, headers=headers(who))


def _pending(admin, org):
    with admin.cursor() as cur:
        cur.execute("SELECT u.email, i.role FROM org_invitations i JOIN users u ON u.user_id = i.invited_user_id "
                    "WHERE i.org_id = %s AND i.accepted_at IS NULL AND i.declined_at IS NULL AND i.revoked_at IS NULL", (org,))
        rows = cur.fetchall()
    admin.rollback()
    return rows


def _user_by_email(admin, email):
    with admin.cursor() as cur:
        cur.execute("SELECT cognito_sub, is_platform_admin, disabled_at FROM users WHERE email = %s", (email,))
        row = cur.fetchone()
    admin.rollback()
    return row


# --- inviting -------------------------------------------------------------------


def test_new_person_sequence_precheck_then_cognito_then_database_then_email(client, world, cognito):
    people, headers, admin = world
    org = _org_with_admin(client, world)
    email = _new_email(people)
    seen_at_send = {}
    cognito.on_resend = lambda username: seen_at_send.update(rows=_pending(admin, org))

    resp = _invite(client, headers, org, email.upper(), display_name="New Person")
    assert resp.status_code == 202 and resp.json() == {"status": "invited"}
    assert cognito.names() == ["find", "create", "resend"]
    assert cognito.calls[0][1] == email  # normalized before Cognito sees it
    sub, is_admin, disabled = _user_by_email(admin, email)
    assert sub == cognito.accounts[email].sub and is_admin is False and disabled is None
    # The email goes out only after the invitation is committed.
    assert seen_at_send["rows"] == [(email, "contributor")]


def test_every_kind_of_target_gets_the_same_answer(client, world, cognito):
    people, headers, admin = world
    org = _org_with_admin(client, world)
    _add_member(world, org, "staff", "contributor")
    for who in ("staff", "pa", "outsider"):
        cognito.accounts[people[who]["email"]] = CognitoAccount(
            sub=str(uuid.uuid4()), username=who, enabled=True, status="CONFIRMED", email_verified=True)
    answers = []
    for target in (_new_email(people), people["staff"]["email"], people["pa"]["email"], people["outsider"]["email"]):
        resp = _invite(client, headers, org, target, role="point_person")
        answers.append((resp.status_code, resp.json()))
    assert answers == [(202, {"status": "invited"})] * 4
    # Every one of them has a pending invitation row, including vendor staff and a current member.
    assert {e for e, _ in _pending(admin, org)} >= {people["staff"]["email"], people["pa"]["email"], people["outsider"]["email"]}


def test_existing_account_gets_no_email(client, world, cognito):
    people, headers, admin = world
    org = _org_with_admin(client, world)
    cognito.accounts[people["staff"]["email"]] = CognitoAccount(
        sub=str(uuid.uuid4()), username="s", enabled=True, status="CONFIRMED", email_verified=True)
    assert _invite(client, headers, org, people["staff"]["email"]).status_code == 202
    assert cognito.names() == ["find"]


def test_repeating_an_open_invitation_does_not_send_a_second_email(client, world, cognito):
    people, headers, admin = world
    org = _org_with_admin(client, world)
    email = _new_email(people)
    assert _invite(client, headers, org, email).status_code == 202
    assert _invite(client, headers, org, email).status_code == 202
    assert cognito.names().count("resend") == 1
    assert len(_pending(admin, org)) == 1


def test_a_stranger_changes_nothing_in_cognito(client, world, cognito):
    people, headers, admin = world
    org = _org_with_admin(client, world)
    email = _new_email(people)
    for who, target_org in (("outsider", org), ("staff", org), ("outsider", str(uuid.uuid4())), ("pa", org)):
        resp = _invite(client, headers, target_org, email, who=who)
        assert resp.status_code == 403
    assert cognito.calls == []
    assert _user_by_email(admin, email) is None


def test_point_person_cannot_invite_an_admin_and_nothing_is_created(client, world, cognito):
    people, headers, admin = world
    org = _org_with_admin(client, world)
    _add_member(world, org, "staff", "point_person")
    resp = _invite(client, headers, org, _new_email(people), role="org_admin", who="staff")
    assert resp.status_code == 403 and cognito.calls == []


def test_cap_stops_before_cognito(client, world, cognito):
    people, headers, admin = world
    org = _org_with_admin(client, world)
    with admin.cursor() as cur:
        for _ in range(10):
            cur.execute("INSERT INTO org_invitations (org_id, invited_user_id, role, invited_by, declined_at) "
                        "VALUES (%s, %s, 'contributor', %s, now())", (org, people["staff"]["id"], people["ed"]["id"]))
    admin.commit()
    resp = _invite(client, headers, org, _new_email(people))
    assert resp.status_code == 429 and cognito.calls == []


def test_cognito_down_gives_502_and_leaves_nothing(client, world, cognito):
    people, headers, admin = world
    org = _org_with_admin(client, world)
    email = _new_email(people)
    cognito.unavailable = True
    resp = _invite(client, headers, org, email)
    assert resp.status_code == 502 and "AWS" not in resp.text
    assert _pending(admin, org) == [] and _user_by_email(admin, email) is None


def test_failed_email_keeps_the_invitation(client, world, cognito):
    people, headers, admin = world
    org = _org_with_admin(client, world)
    email = _new_email(people)
    cognito.resend_fails = True
    assert _invite(client, headers, org, email).status_code == 202
    assert _pending(admin, org) == [(email, "contributor")]


@pytest.mark.parametrize("status_value", ["RESET_REQUIRED", "COMPROMISED", "UNCONFIRMED"])
def test_unusable_cognito_accounts_are_left_alone(client, world, cognito, status_value):
    people, headers, admin = world
    org = _org_with_admin(client, world)
    email = _new_email(people)
    cognito.accounts[email] = CognitoAccount(sub=str(uuid.uuid4()), username="x", enabled=True, status=status_value,
                                             email_verified=True)
    resp = _invite(client, headers, org, email)
    assert resp.status_code == 202 and resp.json() == {"status": "invited"}
    assert "resend" not in cognito.names() and _pending(admin, org) == []


def test_unverified_email_account_is_left_alone(client, world, cognito):
    people, headers, admin = world
    org = _org_with_admin(client, world)
    email = _new_email(people)
    cognito.accounts[email] = CognitoAccount(sub=str(uuid.uuid4()), username="x", enabled=True, status="CONFIRMED",
                                             email_verified=False)
    assert _invite(client, headers, org, email).status_code == 202
    assert "resend" not in cognito.names() and _pending(admin, org) == []
    assert _user_by_email(admin, email) is None  # its sub was never bound to the address


def test_unusable_account_of_a_known_person_counts_like_any_other_invitation(client, world, cognito):
    people, headers, admin = world
    org = _org_with_admin(client, world)
    cognito.accounts[people["staff"]["email"]] = CognitoAccount(
        sub=str(uuid.uuid4()), username="s", enabled=False, status="CONFIRMED", email_verified=True)
    assert _invite(client, headers, org, people["staff"]["email"]).status_code == 202
    assert _pending(admin, org) == [(people["staff"]["email"], "contributor")]  # same cap use, no email
    assert "resend" not in cognito.names()


def test_inviting_yourself_is_refused_and_leaves_nothing(client, world, cognito):
    people, headers, admin = world
    org = _org_with_admin(client, world)
    cognito.accounts[people["ed"]["email"]] = CognitoAccount(
        sub=str(uuid.uuid4()), username="e", enabled=True, status="CONFIRMED", email_verified=True)
    resp = _invite(client, headers, org, people["ed"]["email"])
    assert resp.status_code == 403 and _pending(admin, org) == []


def test_a_burst_of_invitations_cannot_get_past_the_cap(client, world, cognito):
    """The 11th invitation from one sender is refused BEFORE any Cognito
    account is created for it."""
    people, headers, admin = world
    org = _org_with_admin(client, world)
    for n in range(10):
        assert _invite(client, headers, org, _new_email(people, f"n{n}")).status_code == 202
    assert cognito.names().count("create") == 10
    resp = _invite(client, headers, org, _new_email(people, "n11"))
    assert resp.status_code == 429
    assert cognito.names().count("create") == 10


def test_disabled_cognito_account_is_left_alone(client, world, cognito):
    people, headers, admin = world
    org = _org_with_admin(client, world)
    email = _new_email(people)
    cognito.accounts[email] = CognitoAccount(sub=str(uuid.uuid4()), username="x", enabled=False, status="CONFIRMED",
                                             email_verified=True)
    assert _invite(client, headers, org, email).status_code == 202
    assert _pending(admin, org) == []


@pytest.mark.parametrize("body", [
    {"email": "nonsense", "role": "contributor"},
    {"email": "ünï@example.org", "role": "contributor"},
    {"email": "a b@example.org", "role": "contributor"},
    {"email": "x@example.org", "role": "emperor"},
    {"email": "x@example.org", "role": "contributor", "display_name": "tab\there"},
    {"email": "x@example.org", "role": "contributor", "display_name": "x" * 201},
])
def test_bad_input_is_422_before_anything_happens(client, world, cognito, body):
    people, headers, _ = world
    org = _org_with_admin(client, world)
    assert client.post(f"/orgs/{org}/invitations", json=body, headers=headers("ed")).status_code == 422
    assert cognito.calls == []


def test_not_configured_is_503(client, world, monkeypatch):
    people, headers, _ = world
    org = _org_with_admin(client, world)
    for name in ("COGNITO_REGION", "COGNITO_USER_POOL_ID"):
        monkeypatch.delenv(name, raising=False)
    assert _invite(client, headers, org, "someone@example.org").status_code == 503


# --- lists, accept, decline, revoke ------------------------------------------------


def test_my_invitations_accept_and_decline(client, world, cognito):
    people, headers, admin = world
    org = _org_with_admin(client, world)
    for who in ("staff", "outsider"):
        cognito.accounts[people[who]["email"]] = CognitoAccount(
            sub=str(uuid.uuid4()), username=who, enabled=True, status="CONFIRMED", email_verified=True)
    assert _invite(client, headers, org, people["staff"]["email"], role="budget_owner").status_code == 202
    assert _invite(client, headers, org, people["outsider"]["email"], role="contributor").status_code == 202

    mine = client.get("/me/invitations", headers=headers("staff")).json()
    assert [(m["role"], m["invited_by_name"]) for m in mine] == [("budget_owner", people["ed"]["email"])]
    assert mine[0]["org_legal_name"].startswith("Test Org")
    assert client.get("/me/invitations", headers=headers("ed")).json() == []

    invitation = mine[0]["invitation_id"]
    assert client.post(f"/invitations/{invitation}/accept", headers=headers("outsider")).status_code == 404  # not theirs
    assert client.post(f"/invitations/{invitation}/accept", headers=headers("staff")).status_code == 204
    assert client.post(f"/invitations/{invitation}/accept", headers=headers("staff")).status_code == 404  # once
    members = client.get(f"/orgs/{org}/memberships", headers=headers("ed")).json()
    assert (people["staff"]["email"], "budget_owner") in {(m["email"], m["role"]) for m in members}
    assert client.get("/me/invitations", headers=headers("staff")).json() == []

    other = client.get("/me/invitations", headers=headers("outsider")).json()[0]["invitation_id"]
    assert client.post(f"/invitations/{other}/decline", headers=headers("outsider")).status_code == 204
    assert client.post(f"/invitations/{other}/accept", headers=headers("outsider")).status_code == 404
    assert client.post(f"/invitations/{uuid.uuid4()}/accept", headers=headers("staff")).status_code == 404
    assert client.post(f"/invitations/{uuid.uuid4()}/decline", headers=headers("staff")).status_code == 404


def test_org_admin_lists_flags_stale_and_revokes(client, world, cognito):
    people, headers, admin = world
    org = _org_with_admin(client, world)
    mid = _add_member(world, org, "staff", "point_person")
    cognito.accounts[people["outsider"]["email"]] = CognitoAccount(
        sub=str(uuid.uuid4()), username="o", enabled=True, status="CONFIRMED", email_verified=True)
    assert _invite(client, headers, org, people["outsider"]["email"], who="staff").status_code == 202

    listed = client.get(f"/orgs/{org}/invitations", headers=headers("ed")).json()
    assert [(i["email"], i["invited_by_email"], i["stale"]) for i in listed] == [
        (people["outsider"]["email"], people["staff"]["email"], False)]
    assert client.get(f"/orgs/{org}/invitations", headers=headers("outsider")).status_code == 403

    # The sender loses their role: the invitation is flagged and can no longer be accepted.
    assert client.delete(f"/orgs/{org}/memberships/{mid}", headers=headers("ed")).status_code == 204
    listed = client.get(f"/orgs/{org}/invitations", headers=headers("ed")).json()
    assert listed[0]["stale"] is True
    invitation = listed[0]["invitation_id"]
    assert client.post(f"/invitations/{invitation}/accept", headers=headers("outsider")).status_code == 404

    other_org = _org_with_admin(client, world)
    assert client.delete(f"/orgs/{other_org}/invitations/{invitation}", headers=headers("ed")).status_code == 404
    assert client.delete(f"/orgs/{org}/invitations/{invitation}", headers=headers("ed")).status_code == 204
    assert client.get(f"/orgs/{org}/invitations", headers=headers("ed")).json() == []
    assert client.delete(f"/orgs/{org}/invitations/{invitation}", headers=headers("ed")).status_code == 404


# --- sign-in is required ----------------------------------------------------------


@pytest.mark.parametrize("method,path", [
    ("POST", f"/orgs/{uuid.uuid4()}/invitations"), ("GET", f"/orgs/{uuid.uuid4()}/invitations"),
    ("DELETE", f"/orgs/{uuid.uuid4()}/invitations/{uuid.uuid4()}"), ("GET", "/me/invitations"),
    ("POST", f"/invitations/{uuid.uuid4()}/accept"), ("POST", f"/invitations/{uuid.uuid4()}/decline"),
])
def test_every_invitation_route_needs_sign_in(client, cognito_env, cognito, method, path):  # noqa: F811
    assert client.request(method, path).status_code == 401
