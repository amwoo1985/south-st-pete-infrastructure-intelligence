"""The operator's bootstrap tool (app/admin/cli.py): what it refuses, what it
records, and that a dry run saves nothing. Runs against the real local
database as the privileged (migration) login, like a deployed run does."""

from __future__ import annotations

import uuid

import pytest

from app.admin import cli
from app.db.connection import get_migration_connection
from tests.conftest import as_system_job


@pytest.fixture(autouse=True)
def _no_other_platform_admins():
    """The first platform admin needs no operator only while none exists, so
    park any that other tests or earlier runs left in the shared local
    database, and restore them afterwards."""
    conn = as_system_job(get_migration_connection(), "bootstrap test: park platform admins")
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET disabled_at = now() WHERE is_platform_admin AND disabled_at IS NULL RETURNING user_id")
        parked = [r[0] for r in cur.fetchall()]
    conn.commit()
    conn.close()
    yield
    conn = as_system_job(get_migration_connection(), "bootstrap test: restore platform admins")
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET disabled_at = NULL WHERE user_id = ANY(%s)", (parked,))
    conn.commit()
    conn.close()


@pytest.fixture
def tag():
    value = uuid.uuid4().hex[:8]
    yield value
    conn = as_system_job(get_migration_connection(), "bootstrap test cleanup")
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM users WHERE email LIKE %s", (f"%-{value}@example.org",))
        ids = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT org_id FROM organizations WHERE legal_name LIKE %s", (f"%{value}%",))
        orgs = [r[0] for r in cur.fetchall()]
        cur.execute("DELETE FROM org_invitations WHERE org_id = ANY(%s)", (orgs,))
        cur.execute("DELETE FROM org_memberships WHERE org_id = ANY(%s)", (orgs,))
        cur.execute("DELETE FROM audit_log WHERE org_id = ANY(%s) OR actor_user_id = ANY(%s) OR object_id = ANY(%s)",
                    (orgs, ids, [str(i) for i in ids]))
        cur.execute("DELETE FROM organizations WHERE org_id = ANY(%s)", (orgs,))
        cur.execute("DELETE FROM users WHERE user_id = ANY(%s)", (ids,))
    conn.commit()
    conn.close()


def run(*argv):
    """Runs the tool for real: its own connection, committing with --apply."""
    return cli.main(list(argv))


def q(sql, params=()):
    conn = get_migration_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def person(tag, name):
    return f"{name}-{tag}@example.org"


def make_user(tag, name, *, platform_admin=False, operator=None):
    args = ["create-user", "--cognito-sub", str(uuid.uuid4()), "--email", person(tag, name), "--reason", "test setup", "--apply"]
    if platform_admin:
        args.append("--platform-admin")
    if operator:
        args += ["--as", person(tag, operator)]
    assert run(*args) == 0


def make_org(tag, operator="amber", name=None):
    name = name or f"Test Org {tag}"
    assert run("create-org", "--legal-name", name, "--as", person(tag, operator), "--reason", "test setup", "--apply") == 0
    return q("SELECT org_id FROM organizations WHERE legal_name = %s", (name,))[0][0]


# --- create-user ---------------------------------------------------------------


def test_dry_run_saves_nothing_and_apply_saves_with_a_reason(tag, capsys):
    args = ["create-user", "--cognito-sub", str(uuid.uuid4()), "--email", person(tag, "amber"), "--platform-admin",
            "--reason", "bootstrap the vendor admin"]
    assert run(*args) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert q("SELECT 1 FROM users WHERE email = %s", (person(tag, "amber"),)) == []
    assert run(*args, "--apply") == 0
    assert q("SELECT is_platform_admin FROM users WHERE email = %s", (person(tag, "amber"),)) == [(True,)]
    audit = q("SELECT actor_user_id, detail->>'system_job', detail->>'source' FROM audit_log "
              "WHERE action = 'user.create' AND detail->>'email' = %s", (person(tag, "amber"),))
    assert audit == [(None, "bootstrap the vendor admin", "system")]


def test_create_user_twice_is_a_no_op_and_conflicts_are_refused(tag, capsys):
    sub = str(uuid.uuid4())
    args = ["create-user", "--cognito-sub", sub, "--email", person(tag, "erica"), "--reason", "r", "--apply"]
    assert run(*args) == 0
    capsys.readouterr()
    assert run(*args) == 0 and "nothing to do" in capsys.readouterr().out
    assert len(q("SELECT 1 FROM users WHERE email = %s", (person(tag, "erica"),))) == 1
    # Same address with a different account id; same account id with a different address; same but now a platform admin.
    for other in (["--cognito-sub", str(uuid.uuid4()), "--email", person(tag, "erica")],
                  ["--cognito-sub", sub, "--email", person(tag, "other")],
                  ["--cognito-sub", sub, "--email", person(tag, "erica"), "--platform-admin"]):
        assert run("create-user", *other, "--reason", "r", "--apply") == 1
        assert "different account" in capsys.readouterr().err


@pytest.mark.parametrize("sub,email", [("not-a-uuid", "x@example.org"), (str(uuid.uuid4()), "no spaces@example.org"),
                                      (str(uuid.uuid4()), "ünï@example.org"), (str(uuid.uuid4()), "nonsense")])
def test_bad_input_is_refused(tag, sub, email):
    assert run("create-user", "--cognito-sub", sub, "--email", email, "--reason", "r", "--apply") == 1


def test_a_reason_is_required_and_must_be_plain(tag):
    assert run("create-user", "--cognito-sub", str(uuid.uuid4()), "--email", person(tag, "x")) == 2
    assert run("create-user", "--cognito-sub", str(uuid.uuid4()), "--email", person(tag, "x"), "--reason", "   ") == 1
    assert run("create-user", "--cognito-sub", str(uuid.uuid4()), "--email", person(tag, "x"), "--reason", "a\nb") == 1


def test_repeating_the_first_admin_command_is_still_a_no_op(tag, capsys):
    args = ["create-user", "--cognito-sub", str(uuid.uuid4()), "--email", person(tag, "amber"), "--platform-admin",
            "--reason", "bootstrap", "--apply"]
    assert run(*args) == 0
    capsys.readouterr()
    assert run(*args) == 0 and "nothing to do" in capsys.readouterr().out


def test_the_org_name_lock_really_serializes(tag):
    name = f"Locked Org {tag}"
    make_user(tag, "amber", platform_admin=True)
    holder = get_migration_connection()
    with holder.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("org-name:" + name.lower(),))
    other = get_migration_connection()
    with other.cursor() as cur:
        cur.execute("SET statement_timeout = 500")
    with pytest.raises(Exception, match="statement timeout"):
        cli.create_org(other, legal_name=name, operator_email=person(tag, "amber"), reason="r")
    other.rollback()
    holder.rollback()
    holder.close()
    other.close()
    assert q("SELECT 1 FROM organizations WHERE legal_name = %s", (name,)) == []


def test_only_the_very_first_platform_admin_needs_no_operator(tag, capsys):
    make_user(tag, "amber", platform_admin=True)
    second = ["create-user", "--cognito-sub", str(uuid.uuid4()), "--email", person(tag, "vendor"), "--platform-admin",
              "--reason", "r", "--apply"]
    assert run(*second) == 1
    assert "needs --as" in capsys.readouterr().err
    assert run(*second, "--as", person(tag, "nobody")) == 1  # not a platform admin
    assert run(*second, "--as", person(tag, "amber")) == 0
    actor = q("SELECT a.actor_user_id = u.user_id FROM audit_log a JOIN users u ON u.email = %s "
              "WHERE a.action = 'user.create' AND a.detail->>'email' = %s", (person(tag, "amber"), person(tag, "vendor")))
    assert actor == [(True,)]


def test_a_dry_run_leaves_no_audit_rows_either(tag):
    make_user(tag, "amber", platform_admin=True)
    before = q("SELECT count(*) FROM audit_log")[0][0]
    assert run("create-org", "--legal-name", f"Dry Org {tag}", "--as", person(tag, "amber"), "--reason", "r") == 0
    assert q("SELECT count(*) FROM audit_log")[0][0] == before


def test_abbreviated_flags_are_not_accepted(tag):
    # "--app" must not quietly mean --apply.
    assert run("create-user", "--cognito-sub", str(uuid.uuid4()), "--email", person(tag, "x"), "--reason", "r", "--app") == 2
    assert q("SELECT 1 FROM users WHERE email = %s", (person(tag, "x"),)) == []


def test_invisible_formatting_characters_are_refused(tag):
    for reason in ("fine\u202eevil", "zero\u200bwidth"):
        assert run("create-user", "--cognito-sub", str(uuid.uuid4()), "--email", person(tag, "x"), "--reason", reason) == 1


def test_concurrent_create_org_for_one_name_makes_one(tag):
    import threading

    make_user(tag, "amber", platform_admin=True)
    name = f"Race Org {tag}"
    codes = []
    barrier = threading.Barrier(2)

    def go():
        barrier.wait()
        codes.append(run("create-org", "--legal-name", name, "--as", person(tag, "amber"), "--reason", "r", "--apply"))

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes == [0, 0]
    assert len(q("SELECT 1 FROM organizations WHERE legal_name = %s", (name,))) == 1


def test_unexpected_failures_never_show_a_traceback_or_details(tag, capsys, monkeypatch):
    def boom(*a, **k):
        raise ValueError("password=hunter2")

    monkeypatch.setattr(cli, "create_org", boom)
    assert run("create-org", "--legal-name", "x", "--as", "a@example.org", "--reason", "r", "--apply") == 1
    err = capsys.readouterr().err
    assert "hunter2" not in err and "Traceback" not in err and "nothing was saved" in err


# --- create-org ------------------------------------------------------------------


def test_create_org_needs_a_platform_admin_operator(tag, capsys):
    make_user(tag, "amber", platform_admin=True)
    make_user(tag, "erica")
    for operator in (person(tag, "erica"), person(tag, "nobody")):
        assert run("create-org", "--legal-name", f"Org {tag}", "--as", operator, "--reason", "r", "--apply") == 1
    assert "not an active platform admin" in capsys.readouterr().err
    assert q("SELECT 1 FROM organizations WHERE legal_name LIKE %s", (f"%{tag}%",)) == []
    conn = as_system_job(get_migration_connection(), "test disable")
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET disabled_at = now() WHERE email = %s", (person(tag, "amber"),))
    conn.commit()
    conn.close()
    assert run("create-org", "--legal-name", f"Org {tag}", "--as", person(tag, "amber"), "--reason", "r", "--apply") == 1


def test_create_org_records_the_operator_and_is_idempotent(tag, capsys):
    make_user(tag, "amber", platform_admin=True)
    name = f"Florida Food Policy Council {tag}"
    assert run("create-org", "--legal-name", name, "--as", person(tag, "amber"), "--reason", "first customer") == 0
    assert q("SELECT 1 FROM organizations WHERE legal_name = %s", (name,)) == []  # dry run
    org = make_org(tag, name=name)
    assert q("SELECT u.email, o.status, o.billing_account_id FROM organizations o JOIN users u ON u.user_id = o.created_by "
             "WHERE o.org_id = %s", (org,)) == [(person(tag, "amber"), "active", None)]
    audit = q("SELECT a.actor_user_id = u.user_id, a.detail->>'system_job', a.detail->>'source' FROM audit_log a "
              "JOIN users u ON u.email = %s WHERE a.action = 'org.create' AND a.org_id = %s", (person(tag, "amber"), org))
    assert audit == [(True, "test setup", "system")]
    capsys.readouterr()
    assert run("create-org", "--legal-name", name.upper(), "--as", person(tag, "amber"), "--reason", "again", "--apply") == 0
    assert "nothing to do" in capsys.readouterr().out
    assert len(q("SELECT 1 FROM organizations WHERE legal_name ILIKE %s", (name,))) == 1


# --- grant-org-admin -----------------------------------------------------------------


def test_the_bootstrap_story_amber_then_erica(tag):
    make_user(tag, "amber", platform_admin=True)
    make_user(tag, "erica")
    org = make_org(tag)
    assert run("grant-org-admin", "--org", str(org), "--user", person(tag, "erica"), "--as", person(tag, "amber"),
               "--reason", "Erica is the FLFPC administrator", "--apply") == 0
    rows = q("SELECT m.role, g.email, a.action, a.actor_user_id = g.user_id, a.detail->>'source', a.detail->>'system_job' "
             "FROM org_memberships m JOIN users e ON e.user_id = m.user_id "
             "JOIN users g ON g.user_id = m.granted_by "
             "JOIN audit_log a ON a.object_id = m.membership_id::text AND a.action = 'membership.grant' "
             "WHERE m.org_id = %s AND e.email = %s", (org, person(tag, "erica")))
    assert rows == [("org_admin", person(tag, "amber"), "membership.grant", True, "system", "Erica is the FLFPC administrator")]
    assert q("SELECT app_is_member(%s, e.user_id, ARRAY['org_admin']), app_is_platform_admin(e.user_id) FROM users e "
             "WHERE e.email = %s", (org, person(tag, "erica"))) == [(True, False)]


def test_grant_org_admin_refusals(tag, capsys):
    make_user(tag, "amber", platform_admin=True)
    make_user(tag, "erica")
    make_user(tag, "vendor", platform_admin=True, operator="amber")
    org = make_org(tag)
    ok = ["--as", person(tag, "amber"), "--reason", "r", "--apply"]
    # a platform admin can never be a member
    assert run("grant-org-admin", "--org", str(org), "--user", person(tag, "vendor"), *ok) == 1
    assert "platform admin" in capsys.readouterr().err
    # unknown person, no such org, bad id, an operator who is not a platform admin
    assert run("grant-org-admin", "--org", str(org), "--user", person(tag, "ghost"), *ok) == 1
    assert run("grant-org-admin", "--org", str(uuid.uuid4()), "--user", person(tag, "erica"), *ok) == 1
    assert run("grant-org-admin", "--org", "nonsense", "--user", person(tag, "erica"), *ok) == 1
    assert run("grant-org-admin", "--org", str(org), "--user", person(tag, "erica"), "--as", person(tag, "erica"),
               "--reason", "r", "--apply") == 1
    # a suspended organization
    conn = as_system_job(get_migration_connection(), "test suspend")
    with conn.cursor() as cur:
        cur.execute("UPDATE organizations SET status = 'suspended' WHERE org_id = %s", (org,))
    conn.commit()
    conn.close()
    assert run("grant-org-admin", "--org", str(org), "--user", person(tag, "erica"), *ok) == 1
    assert "not active" in capsys.readouterr().err
    assert q("SELECT 1 FROM org_memberships WHERE org_id = %s", (org,)) == []


def test_grant_org_admin_dry_run_and_repeat(tag, capsys):
    make_user(tag, "amber", platform_admin=True)
    make_user(tag, "erica")
    org = make_org(tag)
    args = ["grant-org-admin", "--org", str(org), "--user", person(tag, "erica"), "--as", person(tag, "amber"), "--reason", "r"]
    assert run(*args) == 0 and "DRY RUN" in capsys.readouterr().out
    assert q("SELECT 1 FROM org_memberships WHERE org_id = %s", (org,)) == []
    assert run(*args, "--apply") == 0
    capsys.readouterr()
    assert run(*args, "--apply") == 0 and "already an org admin" in capsys.readouterr().out
    assert len(q("SELECT 1 FROM org_memberships WHERE org_id = %s AND revoked_at IS NULL", (org,))) == 1


def test_grant_refusals_for_disabled_people_closed_orgs_and_disabled_operators(tag, capsys):
    make_user(tag, "amber", platform_admin=True)
    make_user(tag, "erica")
    make_user(tag, "gone")
    org = make_org(tag)
    closed = make_org(tag, name=f"Closed Org {tag}")
    conn = as_system_job(get_migration_connection(), "test states")
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET disabled_at = now() WHERE email = %s", (person(tag, "gone"),))
        cur.execute("UPDATE organizations SET status = 'closed' WHERE org_id = %s", (closed,))
    conn.commit()
    conn.close()
    base = ["--as", person(tag, "amber"), "--reason", "r", "--apply"]
    assert run("grant-org-admin", "--org", str(org), "--user", person(tag, "gone"), *base) == 1
    assert "no active account" in capsys.readouterr().err
    assert run("grant-org-admin", "--org", str(closed), "--user", person(tag, "erica"), *base) == 1
    assert "closed" in capsys.readouterr().err
    conn = as_system_job(get_migration_connection(), "test operator disabled")
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET disabled_at = now() WHERE email = %s", (person(tag, "amber"),))
    conn.commit()
    conn.close()
    assert run("grant-org-admin", "--org", str(org), "--user", person(tag, "erica"), *base) == 1
    assert "not an active platform admin" in capsys.readouterr().err
    assert q("SELECT 1 FROM org_memberships WHERE org_id = ANY(%s)", ([org, closed],)) == []


def test_recovery_admin_can_be_seated_beside_an_existing_one(tag):
    make_user(tag, "amber", platform_admin=True)
    for name in ("erica", "second"):
        make_user(tag, name)
    org = make_org(tag)
    for name in ("erica", "second"):
        assert run("grant-org-admin", "--org", str(org), "--user", person(tag, name), "--as", person(tag, "amber"),
                   "--reason", "recovery", "--apply") == 0
    assert len(q("SELECT 1 FROM org_memberships WHERE org_id = %s AND role = 'org_admin' AND revoked_at IS NULL", (org,))) == 2


def test_nothing_secret_is_printed(tag, capsys):
    import os

    make_user(tag, "amber", platform_admin=True)
    assert run("create-org", "--legal-name", "x" * 400, "--as", person(tag, "amber"), "--reason", "r") == 1
    run("create-user", "--cognito-sub", str(uuid.uuid4()), "--email", person(tag, "amber"), "--reason", "r", "--apply")
    run("grant-org-admin", "--org", str(uuid.uuid4()), "--user", person(tag, "amber"), "--as", person(tag, "amber"), "--reason", "r")
    out = capsys.readouterr()
    for name in ("LOCAL_DB_PASSWORD", "MIGRATION_DB_PASSWORD", "OPENAI_API_KEY"):
        value = os.environ.get(name)
        if value:
            assert value not in out.out + out.err


def test_unreachable_database_is_reported_without_details(capsys):
    def broken():
        raise RuntimeError("LOCAL_DB_PASSWORD is not set; value=hunter2")

    code = cli.main(["create-user", "--cognito-sub", str(uuid.uuid4()), "--email", "x@example.org", "--reason", "r"], connect=broken)
    assert code == 1
    assert "hunter2" not in capsys.readouterr().err
