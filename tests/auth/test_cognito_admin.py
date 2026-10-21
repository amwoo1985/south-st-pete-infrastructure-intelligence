"""app/auth/cognito_admin.py against a stand-in for boto3's client: what we
ask Cognito for, and how its failures are reduced. No network."""

from __future__ import annotations

import uuid

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app.auth import cognito_admin
from app.auth.cognito_admin import CognitoAdmin, CognitoRefused, CognitoUnavailable

POOL = "us-east-1_TESTPOOL"


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "secret aws text"}}, "Op")


def _user(email="x@example.org", status="FORCE_CHANGE_PASSWORD", enabled=True, sub=None, verified=True):
    attrs = [{"Name": "sub", "Value": sub or str(uuid.uuid4())}, {"Name": "email", "Value": email}]
    if verified is not None:
        attrs.append({"Name": "email_verified", "Value": "true" if verified else "false"})
    return {"Username": "gen-" + uuid.uuid4().hex, "Enabled": enabled, "UserStatus": status, "Attributes": attrs}


class StubClient:
    def __init__(self):
        self.calls = []
        self.list_replies = []
        self.create_reply = None
        self.create_error = None
        self.list_error = None

    def list_users(self, **kw):
        self.calls.append(("list_users", kw))
        if self.list_error:
            raise self.list_error
        return self.list_replies.pop(0) if self.list_replies else {"Users": []}

    def admin_create_user(self, **kw):
        self.calls.append(("admin_create_user", kw))
        if self.create_error:
            raise self.create_error
        return self.create_reply or {"User": _user()}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(cognito_admin.time, "sleep", lambda s: None)


def test_find_uses_an_exact_email_filter_and_returns_the_account():
    stub = StubClient()
    user = _user(status="CONFIRMED")
    stub.list_replies = [{"Users": [user]}]
    account = CognitoAdmin(stub, POOL).find_by_email("x@example.org")
    assert account.username == user["Username"] and account.status == "CONFIRMED" and account.can_be_invited
    name, kw = stub.calls[0]
    assert name == "list_users" and kw["UserPoolId"] == POOL and kw["Filter"] == 'email = "x@example.org"'


def test_find_returns_none_when_there_is_no_account():
    assert CognitoAdmin(StubClient(), POOL).find_by_email("x@example.org") is None


@pytest.mark.parametrize("bad", ['a"b@example.org', "a\b@example.org", "a b@example.org", 'x@example.org" or email ^= "', "ünï@example.org"])
def test_unsafe_addresses_never_reach_the_filter(bad):
    stub = StubClient()
    with pytest.raises(CognitoRefused):
        CognitoAdmin(stub, POOL).find_by_email(bad)
    assert stub.calls == []


def test_a_malformed_reply_is_unavailable_not_a_crash():
    stub = StubClient()
    stub.list_replies = [{"Users": [{"Attributes": [{"Name": "sub"}]}]}]
    with pytest.raises(CognitoUnavailable):
        CognitoAdmin(stub, POOL).find_by_email("x@example.org")
    stub = StubClient()
    stub.create_reply = {"Unexpected": True}
    with pytest.raises(CognitoUnavailable):
        CognitoAdmin(stub, POOL).create_without_message("x@example.org")


def test_two_accounts_for_one_address_is_treated_as_unavailable():
    stub = StubClient()
    stub.list_replies = [{"Users": [_user(), _user()]}]
    with pytest.raises(CognitoUnavailable):
        CognitoAdmin(stub, POOL).find_by_email("x@example.org")


def test_an_account_without_a_usable_sub_is_unavailable():
    stub = StubClient()
    stub.list_replies = [{"Users": [_user(sub="not-a-uuid")]}]
    with pytest.raises(CognitoUnavailable):
        CognitoAdmin(stub, POOL).find_by_email("x@example.org")


def test_create_sends_nothing_and_verifies_the_email_attribute():
    stub = StubClient()
    account = CognitoAdmin(stub, POOL).create_without_message("x@example.org")
    kw = stub.calls[0][1]
    assert kw["MessageAction"] == "SUPPRESS" and kw["Username"] == "x@example.org"
    assert {"Name": "email", "Value": "x@example.org"} in kw["UserAttributes"]
    assert {"Name": "email_verified", "Value": "true"} in kw["UserAttributes"]
    assert account.status == "FORCE_CHANGE_PASSWORD"


def test_create_race_looks_the_account_up_again():
    stub = StubClient()
    stub.create_error = _client_error("UsernameExistsException")
    stub.list_replies = [{"Users": []}, {"Users": [_user(status="CONFIRMED")]}]
    account = CognitoAdmin(stub, POOL).create_without_message("x@example.org")
    assert account.status == "CONFIRMED"


def test_create_race_that_never_resolves_is_unavailable():
    stub = StubClient()
    stub.create_error = _client_error("UsernameExistsException")
    with pytest.raises(CognitoUnavailable):
        CognitoAdmin(stub, POOL).create_without_message("x@example.org")


def test_resend_asks_for_a_resend_to_email():
    stub = StubClient()
    CognitoAdmin(stub, POOL).resend_invitation("gen-abc")
    kw = stub.calls[0][1]
    assert kw["MessageAction"] == "RESEND" and kw["Username"] == "gen-abc" and kw["DesiredDeliveryMediums"] == ["EMAIL"]


@pytest.mark.parametrize("code", ["TooManyRequestsException", "InternalErrorException", "NotAuthorizedException", "ResourceNotFoundException"])
def test_cognito_failures_become_unavailable_without_aws_text(code):
    stub = StubClient()
    stub.list_error = _client_error(code)
    with pytest.raises(CognitoUnavailable) as exc:
        CognitoAdmin(stub, POOL).find_by_email("x@example.org")
    assert "secret aws text" not in str(exc.value)


def test_network_failure_is_unavailable():
    stub = StubClient()
    stub.list_error = EndpointConnectionError(endpoint_url="https://cognito-idp.example")
    with pytest.raises(CognitoUnavailable):
        CognitoAdmin(stub, POOL).find_by_email("x@example.org")


def test_a_rejected_parameter_is_our_problem_not_the_callers():
    stub = StubClient()
    stub.create_error = _client_error("InvalidParameterException")
    with pytest.raises(CognitoUnavailable):
        CognitoAdmin(stub, POOL).create_without_message("x@example.org")


def test_created_accounts_count_as_verified_even_if_the_reply_omits_it():
    stub = StubClient()
    stub.create_reply = {"User": _user(verified=None)}
    assert CognitoAdmin(stub, POOL).create_without_message("x@example.org").can_be_invited


def test_a_found_account_with_an_unverified_email_is_not_invitable():
    stub = StubClient()
    stub.list_replies = [{"Users": [_user(status="CONFIRMED", verified=False)]}]
    assert CognitoAdmin(stub, POOL).find_by_email("x@example.org").can_be_invited is False


def test_the_race_lookup_happens_before_any_sleep(monkeypatch):
    stub = StubClient()
    stub.create_error = _client_error("UsernameExistsException")
    stub.list_replies = [{"Users": [_user(status="CONFIRMED")]}]
    slept = []
    monkeypatch.setattr(cognito_admin.time, "sleep", lambda s: slept.append(s))
    CognitoAdmin(stub, POOL).create_without_message("x@example.org")
    assert slept == []


@pytest.mark.parametrize("status,enabled,ok", [
    ("CONFIRMED", True, True), ("FORCE_CHANGE_PASSWORD", True, True),
    ("CONFIRMED", False, False), ("RESET_REQUIRED", True, False), ("COMPROMISED", True, False), ("UNCONFIRMED", True, False),
])
def test_only_usable_accounts_can_be_invited(status, enabled, ok):
    stub = StubClient()
    stub.list_replies = [{"Users": [_user(status=status, enabled=enabled)]}]
    assert CognitoAdmin(stub, POOL).find_by_email("x@example.org").can_be_invited is ok


def test_unconfigured_pool_fails_closed(monkeypatch):
    for name in (cognito_admin.REGION_ENV, cognito_admin.POOL_ENV):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(CognitoUnavailable):
        cognito_admin.get_cognito_admin()
    monkeypatch.setenv(cognito_admin.REGION_ENV, "us-east-1")
    monkeypatch.setenv(cognito_admin.POOL_ENV, "changeme")
    with pytest.raises(CognitoUnavailable):
        cognito_admin.get_cognito_admin()
