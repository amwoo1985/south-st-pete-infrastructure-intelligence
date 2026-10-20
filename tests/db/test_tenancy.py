"""Tenancy rules (migration 0005, DECISIONS #166), exercised through a real
app_runtime login acting as specific people, the way the deployed API will.
Every denial asserts the specific reason, so a test can't pass because of an
unrelated error. Setup and cleanup use the privileged role with a named
system job."""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app.db import migrate
from app.db.connection import get_migration_connection
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
def world(runtime_password):
    """People, plus factories for connections acting as them (app_runtime,
    or the privileged role without a system job). Cleaned up afterwards."""
    tag = uuid.uuid4().hex[:8]
    admin = as_system_job(get_migration_connection(), "tenancy test setup")
    people = {}
    with admin.cursor() as cur:
        for name, platform_admin, disabled in (
            ("pa1", True, False), ("pa2", True, False),
            ("a", False, False), ("b", False, False), ("c", False, False), ("d_disabled", False, True),
        ):
            cur.execute(
                "INSERT INTO users (cognito_sub, email, is_platform_admin, disabled_at) "
                "VALUES (%s, %s, %s, CASE WHEN %s THEN now() END) RETURNING user_id",
                (f"sub-{name}-{tag}", f"{name}-{tag}@example.org", platform_admin, disabled),
            )
            people[name] = cur.fetchone()[0]
    admin.commit()
    opened = []

    def _identify(conn, name):
        if name is not None:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.user_id', %s, false)", (str(people[name]),))
            conn.commit()
        opened.append(conn)
        return conn

    def act_as(name: str | None):
        conn = psycopg.connect(
            host=os.environ.get("LOCAL_DB_HOST", "127.0.0.1"),
            port=os.environ.get("LOCAL_DB_PORT", "5432"),
            user=migrate.RUNTIME_ROLE,
            password=runtime_password,
            dbname=os.environ["LOCAL_DB_NAME"],
        )
        return _identify(conn, name)

    def privileged_as(name: str | None):
        """The privileged role WITHOUT a system job: must not bypass rules."""
        return _identify(get_migration_connection(), name)

    yield people, act_as, privileged_as, admin

    for conn in opened:
        conn.close()
    ids = list(people.values())
    with admin.cursor() as cur:
        cur.execute("SELECT org_id FROM organizations WHERE created_by = ANY(%s)", (ids,))
        orgs = [r[0] for r in cur.fetchall()]
        cur.execute("DELETE FROM audit_log WHERE org_id = ANY(%s) OR actor_user_id = ANY(%s) OR object_id = ANY(%s)",
                    (orgs, ids, [str(i) for i in ids]))
        cur.execute("DELETE FROM support_access_grants WHERE org_id = ANY(%s)", (orgs,))
        cur.execute("DELETE FROM org_memberships WHERE org_id = ANY(%s)", (orgs,))
        cur.execute("DELETE FROM organizations WHERE org_id = ANY(%s)", (orgs,))
        cur.execute("DELETE FROM users WHERE user_id = ANY(%s)", (ids,))
    admin.commit()
    admin.close()


def _run(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone() if cur.description else None
    conn.commit()
    return row


def _denied(conn, sql, params=(), *, match: str):
    """Must fail, and for the stated reason."""
    with pytest.raises(psycopg.Error, match=match):
        with conn.cursor() as cur:
            cur.execute(sql, params)
    conn.rollback()


def _new_org(act_as, by="pa1"):
    return _run(act_as(by), "INSERT INTO organizations (legal_name) VALUES ('Org ' || gen_random_uuid()) RETURNING org_id")[0]


def _grant(conn, org, user, role):
    return _run(conn, "INSERT INTO org_memberships (org_id, user_id, role) VALUES (%s, %s, %s) RETURNING membership_id",
                (org, user, role))[0]


def _org_with_admin(world):
    people, act_as, _, _ = world
    org = _new_org(act_as)
    _grant(act_as("pa1"), org, people["a"], "org_admin")
    return org


GRANT = "INSERT INTO org_memberships (org_id, user_id, role) VALUES (%s, %s, %s)"
REVOKE = "UPDATE org_memberships SET revoked_at = now() WHERE membership_id = %s"
SUPPORT = ("INSERT INTO support_access_grants (org_id, support_user_id, reason, expires_at) "
           "VALUES (%s, %s, 'help', now() + interval '3 days') RETURNING grant_id")


# --- organizations ----------------------------------------------------------


def test_only_platform_admin_creates_org_and_created_by_is_stamped(world):
    people, act_as, _, admin = world
    _denied(act_as("a"), "INSERT INTO organizations (legal_name) VALUES ('Nope')", match="only a platform admin can create")
    org = _run(act_as("pa1"), "INSERT INTO organizations (legal_name, created_by) VALUES ('Real', %s) RETURNING org_id",
               (people["a"],))[0]
    with admin.cursor() as cur:
        cur.execute("SELECT created_by FROM organizations WHERE org_id = %s", (org,))
        assert cur.fetchone()[0] == people["pa1"]
    admin.rollback()


def test_new_org_always_starts_active_with_no_billing(world):
    people, act_as, _, admin = world
    org = _run(act_as("pa1"), "INSERT INTO organizations (legal_name, status, billing_account_id) "
               "VALUES ('Sneaky', 'suspended', gen_random_uuid()) RETURNING org_id")[0]
    with admin.cursor() as cur:
        cur.execute("SELECT status, billing_account_id FROM organizations WHERE org_id = %s", (org,))
        assert cur.fetchone() == ("active", None)
    admin.rollback()


def test_runtime_without_acting_user_can_do_nothing(world):
    people, act_as, _, _ = world
    _denied(act_as(None), "INSERT INTO organizations (legal_name) VALUES ('Anon')", match="no acting user")


def test_status_changes_platform_admin_only_and_closed_is_terminal(world):
    people, act_as, _, _ = world
    org = _org_with_admin(world)
    _denied(act_as("a"), "UPDATE organizations SET status = 'suspended' WHERE org_id = %s", (org,),
            match="only a platform admin can change")
    _run(act_as("pa1"), "UPDATE organizations SET status = 'closed' WHERE org_id = %s", (org,))
    _denied(act_as("pa1"), "UPDATE organizations SET status = 'active' WHERE org_id = %s", (org,), match="cannot be reopened")


def test_rename_org_admin_only_and_billing_not_writable(world):
    people, act_as, _, _ = world
    org = _org_with_admin(world)
    _run(act_as("a"), "UPDATE organizations SET display_name = 'Renamed' WHERE org_id = %s", (org,))
    _grant(act_as("a"), org, people["b"], "contributor")
    _denied(act_as("b"), "UPDATE organizations SET display_name = 'Hijack' WHERE org_id = %s", (org,),
            match="only an org admin can rename")
    _denied(act_as("a"), "UPDATE organizations SET billing_account_id = gen_random_uuid() WHERE org_id = %s", (org,),
            match="permission denied for table organizations")  # column grant


def test_privileged_role_without_a_reason_gets_no_bypass(world):
    people, act_as, privileged_as, _ = world
    org = _org_with_admin(world)
    _denied(privileged_as("a"), "INSERT INTO organizations (legal_name) VALUES ('NoReason')", match="only a platform admin can create")
    _denied(privileged_as("a"), "UPDATE organizations SET billing_account_id = gen_random_uuid() WHERE org_id = %s", (org,),
            match="billing can only be changed by the privileged path")
    _denied(privileged_as("a"), "UPDATE organizations SET legal_name = 'Renamed Inc' WHERE org_id = %s", (org,),
            match="legal name changes need the privileged path")


# --- memberships: who may grant ---------------------------------------------


def test_platform_admin_appoints_first_admin_only(world):
    people, act_as, _, _ = world
    org = _new_org(act_as)
    _grant(act_as("pa1"), org, people["a"], "org_admin")
    _denied(act_as("pa1"), GRANT, (org, people["b"], "org_admin"), match="not allowed to grant org admin here")
    _denied(act_as("pa2"), GRANT, (org, people["b"], "org_admin"), match="not allowed to grant org admin here")


def test_platform_admin_can_appoint_recovery_admin_when_only_admin_is_disabled(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    _run(admin, "UPDATE users SET disabled_at = now() WHERE user_id = %s", (people["a"],))
    _grant(act_as("pa1"), org, people["b"], "org_admin")


def test_platform_admin_cannot_grant_non_admin_roles(world):
    people, act_as, _, _ = world
    org = _new_org(act_as)
    _denied(act_as("pa1"), GRANT, (org, people["a"], "contributor"), match="not allowed to grant roles")


@pytest.mark.parametrize("grantor_role, role", [
    ("org_admin", "org_admin"), ("org_admin", "contributor"), ("point_person", "point_person"),
])
def test_vendor_staff_never_become_members(world, grantor_role, role):
    """Platform admins get in only through consented, time-limited support
    grants, never as members, whoever tries to add them."""
    people, act_as, _, _ = world
    org = _org_with_admin(world)
    grantor = "a"
    if grantor_role == "point_person":
        _grant(act_as("a"), org, people["c"], "point_person")
        grantor = "c"
    _denied(act_as(grantor), GRANT, (org, people["pa1"], role), match="platform admins get access only through support grants")
    _denied(act_as("pa1"), GRANT, (_new_org(act_as), people["pa2"], "org_admin"),
            match="platform admins get access only through support grants")


def test_org_admin_grants_and_granted_by_cannot_be_forged(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    mid = _run(act_as("a"), "INSERT INTO org_memberships (org_id, user_id, role, granted_by) VALUES (%s, %s, 'contributor', %s) "
               "RETURNING membership_id", (org, people["b"], people["c"]))[0]
    with admin.cursor() as cur:
        cur.execute("SELECT granted_by FROM org_memberships WHERE membership_id = %s", (mid,))
        assert cur.fetchone()[0] == people["a"]
    admin.rollback()


def test_nobody_grants_themselves(world):
    people, act_as, _, _ = world
    org = _org_with_admin(world)
    _denied(act_as("a"), GRANT, (org, people["a"], "contributor"), match="cannot grant yourself")


def test_contributor_cannot_grant_point_person_cannot_grant_admin(world):
    people, act_as, _, _ = world
    org = _org_with_admin(world)
    _grant(act_as("a"), org, people["b"], "contributor")
    _grant(act_as("a"), org, people["c"], "point_person")
    _denied(act_as("b"), GRANT, (org, people["c"], "contributor"), match="not allowed to grant roles")
    _grant(act_as("c"), org, people["b"], "budget_owner")
    _denied(act_as("c"), GRANT, (org, people["b"], "org_admin"), match="not allowed to grant org admin here")


def test_admin_of_one_org_has_no_power_in_another(world):
    people, act_as, _, _ = world
    org_a = _org_with_admin(world)
    org_b = _new_org(act_as)
    _grant(act_as("pa1"), org_b, people["c"], "org_admin")
    _denied(act_as("a"), GRANT, (org_b, people["b"], "contributor"), match="not allowed to grant roles")
    _denied(act_as("a"), "UPDATE organizations SET display_name = 'Mine now' WHERE org_id = %s", (org_b,),
            match="only an org admin can rename")
    assert org_a != org_b


def test_disabled_people_cannot_be_granted(world):
    people, act_as, _, _ = world
    org = _org_with_admin(world)
    _denied(act_as("a"), GRANT, (org, people["d_disabled"], "contributor"), match="account is not active")


def test_no_grants_in_suspended_org(world):
    people, act_as, _, _ = world
    org = _org_with_admin(world)
    _run(act_as("pa1"), "UPDATE organizations SET status = 'suspended' WHERE org_id = %s", (org,))
    _denied(act_as("a"), GRANT, (org, people["b"], "contributor"), match="organization is not active")


# --- memberships: revocation ------------------------------------------------


def test_revocation_rules(world):
    people, act_as, _, _ = world
    org = _org_with_admin(world)
    mb = _grant(act_as("a"), org, people["b"], "contributor")
    mc = _grant(act_as("a"), org, people["c"], "contributor")
    _denied(act_as("b"), REVOKE, (mc,), match="not allowed to remove this membership")
    _denied(act_as("pa1"), REVOKE, (mb,), match="not allowed to remove this membership")
    _run(act_as("b"), REVOKE, (mb,))
    _run(act_as("a"), REVOKE, (mc,))
    _denied(act_as("a"), "UPDATE org_memberships SET revoked_at = NULL WHERE membership_id = %s", (mc,), match="already revoked")


def test_only_revoked_at_is_writable(world):
    people, act_as, _, _ = world
    org = _org_with_admin(world)
    mb = _grant(act_as("a"), org, people["b"], "contributor")
    _denied(act_as("a"), "UPDATE org_memberships SET role = 'org_admin' WHERE membership_id = %s", (mb,),
            match="permission denied for table org_memberships")
    _denied(act_as("a"), "UPDATE org_memberships SET revoked_by = %s, revoked_at = now() WHERE membership_id = %s",
            (people["c"], mb), match="permission denied for table org_memberships")


def test_last_admin_cannot_leave_but_one_of_two_can(world):
    people, act_as, _, _ = world
    org = _new_org(act_as)
    ma = _grant(act_as("pa1"), org, people["a"], "org_admin")
    _denied(act_as("a"), REVOKE, (ma,), match="must keep at least one active admin")
    _grant(act_as("a"), org, people["b"], "org_admin")
    _run(act_as("a"), REVOKE, (ma,))


def test_grant_rules_refuse_repeatable_read(world):
    people, act_as, _, _ = world
    org = _org_with_admin(world)
    conn = act_as("a")
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    _denied(conn, GRANT, (org, people["b"], "contributor"), match="must run at READ COMMITTED")


def test_revoked_member_loses_membership(world):
    people, act_as, _, _ = world
    org = _org_with_admin(world)
    mb = _grant(act_as("a"), org, people["b"], "point_person")
    _run(act_as("a"), REVOKE, (mb,))
    _denied(act_as("b"), GRANT, (org, people["c"], "contributor"), match="not allowed to grant roles")


# --- vendor support access --------------------------------------------------


def test_support_access_consent_rules(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    _denied(act_as("pa1"), SUPPORT, (org, people["pa1"]), match="only an org admin can grant support access")
    _grant(act_as("a"), org, people["b"], "contributor")
    _denied(act_as("b"), SUPPORT, (org, people["pa1"]), match="only an org admin can grant support access")
    _denied(act_as("a"), SUPPORT, (org, people["c"]), match="only go to an active platform admin")
    gid = _run(act_as("a"), SUPPORT, (org, people["pa1"]))[0]
    with admin.cursor() as cur:
        cur.execute("SELECT app_active_support_grant(%s, %s), consented_by FROM support_access_grants WHERE grant_id = %s",
                    (org, people["pa1"], gid))
        active, consented_by = cur.fetchone()
    admin.rollback()
    assert active is True and consented_by == people["a"]
    _run(act_as("a"), "UPDATE support_access_grants SET revoked_at = now() WHERE grant_id = %s", (gid,))
    with admin.cursor() as cur:
        cur.execute("SELECT app_active_support_grant(%s, %s)", (org, people["pa1"]))
        assert cur.fetchone()[0] is False
    admin.rollback()


def test_support_access_no_backdating_and_max_14_days(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    past = datetime.now(timezone.utc) - timedelta(days=30)
    gid = _run(act_as("a"), "INSERT INTO support_access_grants (org_id, support_user_id, reason, starts_at, expires_at) "
               "VALUES (%s, %s, 'help', %s, now() + interval '1 day') RETURNING grant_id", (org, people["pa1"], past))[0]
    with admin.cursor() as cur:
        cur.execute("SELECT starts_at > now() - interval '1 minute' FROM support_access_grants WHERE grant_id = %s", (gid,))
        assert cur.fetchone()[0] is True
    admin.rollback()
    _denied(act_as("a"), "INSERT INTO support_access_grants (org_id, support_user_id, reason, expires_at) "
            "VALUES (%s, %s, 'help', now() + interval '30 days')", (org, people["pa1"]),
            match="support_access_grants_check")


def test_no_support_access_to_closed_org(world):
    people, act_as, _, _ = world
    org = _org_with_admin(world)
    _run(act_as("pa1"), "UPDATE organizations SET status = 'closed' WHERE org_id = %s", (org,))
    _denied(act_as("a"), SUPPORT, (org, people["pa1"]), match="organization is not active")


# --- audit ------------------------------------------------------------------


def test_every_grant_is_audited_with_the_real_actor_and_source(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    mb = _grant(act_as("a"), org, people["b"], "contributor")
    _run(act_as("b"), REVOKE, (mb,))
    with admin.cursor() as cur:
        cur.execute("SELECT action, actor_user_id, detail->>'source' FROM audit_log WHERE org_id = %s ORDER BY audit_id", (org,))
        rows = cur.fetchall()
    admin.rollback()
    assert rows == [
        ("org.create", people["pa1"], "database"),
        ("membership.grant", people["pa1"], "database"),
        ("membership.grant", people["a"], "database"),
        ("membership.revoke", people["b"], "database"),
    ]


def test_runtime_cannot_read_or_write_audit_log_directly(world):
    people, act_as, _, _ = world
    _denied(act_as("a"), "SELECT count(*) FROM audit_log", match="permission denied for table audit_log")
    _denied(act_as("a"), "INSERT INTO audit_log (actor_user_id, action, object_type, object_id) VALUES (%s, 'forged', 'x', 'y')",
            (people["pa1"],), match="permission denied for table audit_log")


def test_api_audit_writes_are_stamped_and_limited(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    other = _new_org(act_as)
    marker = "test-" + uuid.uuid4().hex
    # Allowed: a member logging an app event into their own org.
    _run(act_as("a"), "SELECT audit_write('document.view', 'thing', %s, %s, jsonb_build_object('system_job', 'forged', 'source', 'database'))",
         (marker, org))
    with admin.cursor() as cur:
        cur.execute("SELECT actor_user_id, detail FROM audit_log WHERE object_id = %s", (marker,))
        actor, detail = cur.fetchone()
    admin.rollback()
    assert actor == people["a"]
    assert "system_job" not in detail and detail["source"] == "api"
    # Denied: forging a database-recorded event, or writing into another org.
    _denied(act_as("a"), "SELECT audit_write('membership.grant', 'membership', 'x', %s, '{}'::jsonb)", (org,),
            match="reserved for the database")
    _denied(act_as("a"), "SELECT audit_write('document.view', 'thing', 'x', %s, '{}'::jsonb)", (other,),
            match="cannot write to that organization")


def test_audit_records_are_immutable_without_a_reason(world):
    people, act_as, privileged_as, _ = world
    org = _org_with_admin(world)
    _denied(privileged_as("a"), "DELETE FROM audit_log WHERE org_id = %s", (org,), match="cannot be changed or removed")
    _denied(privileged_as("a"), "UPDATE audit_log SET action = 'x' WHERE org_id = %s", (org,), match="cannot be changed or removed")


def test_users_changes_are_audited_and_need_a_reason(world):
    people, act_as, privileged_as, admin = world
    _denied(privileged_as(None), "UPDATE users SET display_name = 'x' WHERE user_id = %s", (people["c"],),
            match="without an actor or app.system_job reason")
    _run(admin, "UPDATE users SET is_platform_admin = true WHERE user_id = %s", (people["c"],))
    with admin.cursor() as cur:
        cur.execute("SELECT detail FROM audit_log WHERE action = 'user.update' AND object_id = %s ORDER BY audit_id DESC LIMIT 1",
                    (str(people["c"]),))
        detail = cur.fetchone()[0]
    admin.rollback()
    assert detail["changed"] == {"is_platform_admin": [False, True]}
    assert detail["system_job"] == "tenancy test setup" and detail["source"] == "system"


# --- identity survives rollback ---------------------------------------------


def test_acting_user_survives_route_rollback(world):
    from app.auth.context import set_acting_user
    from app.auth.principal import Principal

    people, act_as, _, _ = world
    org = _org_with_admin(world)
    conn = act_as(None)
    set_acting_user(conn, Principal(user_id=people["a"], cognito_sub="x", email="x", display_name=None, is_platform_admin=False))
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
    conn.rollback()
    _grant(conn, org, people["b"], "contributor")


def test_member_promoted_to_platform_admin_loses_member_powers(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    _grant(act_as("a"), org, people["b"], "point_person")
    _run(admin, "UPDATE users SET is_platform_admin = true WHERE user_id = %s", (people["b"],))
    with admin.cursor() as cur:
        cur.execute("SELECT app_is_member(%s, %s, ARRAY['point_person'])", (org, people["b"]))
        assert cur.fetchone()[0] is False
    admin.rollback()
    _denied(act_as("b"), GRANT, (org, people["c"], "contributor"), match="not allowed to grant roles")


@pytest.mark.parametrize("action", ["Membership.grant", " membership.grant", "membership:grant", "MEMBERSHIP.revoke", "free text"])
def test_api_audit_actions_must_be_strict_lowercase(world, action):
    people, act_as, _, _ = world
    org = _org_with_admin(world)
    _denied(act_as("a"), "SELECT audit_write(%s, 'thing', 'x', %s, '{}'::jsonb)", (action, org), match='must look like "area.event"')
