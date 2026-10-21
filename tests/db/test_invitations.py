"""Invitations (migration 0006, DECISIONS #170/#171), exercised through a real
app_runtime login acting as specific people. Inviting grants nothing; a
membership appears only when the invitee accepts. Every denial asserts its
specific reason."""

from __future__ import annotations

import uuid

import psycopg
import pytest

from tests.db.test_tenancy import (  # noqa: F401  (fixtures + helpers shared with the tenancy tests)
    GRANT,
    INVITE,
    REVOKE,
    _accept,
    _denied,
    _email,
    _grant,
    _invite,
    _new_org,
    _org_with_admin,
    _run,
    runtime_password,
    world,
)

ACCEPT = "SELECT accept_invitation(%s)"
DECLINE = "SELECT decline_invitation(%s)"
REVOKE_INVITATION = "SELECT revoke_invitation(%s)"
INVITE_NEW = "SELECT out_invitation_id, out_email, out_created FROM invite_user(%s, %s, %s, %s, %s)"


def _state(admin, invitation):
    with admin.cursor() as cur:
        cur.execute(
            "SELECT accepted_at IS NOT NULL, declined_at IS NOT NULL, revoked_at IS NOT NULL, invited_by "
            "FROM org_invitations WHERE invitation_id = %s", (invitation,))
        row = cur.fetchone()
    admin.rollback()
    return row


def _active_memberships(admin, org, user):
    with admin.cursor() as cur:
        cur.execute("SELECT role, granted_by FROM org_memberships WHERE org_id = %s AND user_id = %s AND revoked_at IS NULL",
                    (org, user))
        rows = cur.fetchall()
    admin.rollback()
    return rows


def _new_address(admin, people, label="newbie"):
    """An address that world's cleanup will find (same tag as the people)."""
    suffix = _email(admin, people["a"]).split("-", 1)[1]
    return f"{label}-{uuid.uuid4().hex[:6]}-{suffix}"


# --- inviting grants nothing; accepting does --------------------------------


def test_inviting_grants_nothing_and_accepting_grants_with_the_inviter_as_granter(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    invitation = _invite(world, "a", org, "b", "contributor")
    assert _active_memberships(admin, org, people["b"]) == []
    assert _state(admin, invitation)[:3] == (False, False, False)

    _accept(world, "b", invitation)
    assert _active_memberships(admin, org, people["b"]) == [("contributor", people["a"])]
    assert _state(admin, invitation)[:3] == (True, False, False)


def test_only_the_invitee_can_accept_or_decline(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    invitation = _invite(world, "a", org, "b", "contributor")
    for who in ("c", "a", "pa1"):
        _denied(act_as(who), ACCEPT, (invitation,), match="no such invitation")
        _denied(act_as(who), DECLINE, (invitation,), match="no such invitation")
    assert _active_memberships(admin, org, people["b"]) == []


def test_an_invitation_works_once(world):
    people, act_as, _, _ = world
    org = _org_with_admin(world)
    invitation = _invite(world, "a", org, "b", "contributor")
    _accept(world, "b", invitation)
    _denied(act_as("b"), ACCEPT, (invitation,), match="no such invitation")
    _denied(act_as("b"), DECLINE, (invitation,), match="no such invitation")


def test_declined_invitation_cannot_be_accepted_and_can_be_reissued(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    first = _invite(world, "a", org, "b", "contributor")
    _run(act_as("b"), DECLINE, (first,))
    assert _state(admin, first)[:3] == (False, True, False)
    _denied(act_as("b"), ACCEPT, (first,), match="no such invitation")
    second = _invite(world, "a", org, "b", "contributor")
    assert second != first
    _accept(world, "b", second)


def test_repeating_an_open_invitation_returns_the_same_one(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    email = _email(admin, people["b"])
    first = _run(act_as("a"), INVITE_NEW, (org, email, "contributor", None, None))
    again = _run(act_as("a"), INVITE_NEW, (org, email.upper(), "contributor", None, None))
    assert first[0] == again[0] and first[2] is True and again[2] is False


# --- who may invite ----------------------------------------------------------


def test_invite_authority_is_checked_before_anything_about_the_target(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    unknown = _new_address(admin, people)
    # A stranger gets the same refusal for a real person, an unknown address,
    # a nonexistent org and a bad address; no cap or account hint leaks.
    answers = set()
    for target_org, email in ((org, _email(admin, people["b"])), (org, unknown), (uuid.uuid4(), unknown), (org, "nonsense")):
        with pytest.raises(psycopg.Error) as exc:
            with act_as("c").cursor() as cur:
                cur.execute(INVITE, (target_org, email, "contributor"))
        act_as("c").rollback()
        answers.add(str(exc.value).splitlines()[0])
    assert answers == {"not allowed to invite people to this organization"}


def test_point_person_invites_non_admin_roles_only(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    _grant(world, "a", org, "c", "point_person")
    _invite(world, "c", org, "b", "budget_owner")
    _denied(act_as("c"), INVITE, (org, _email(admin, people["b"]), "org_admin"), match="not allowed to invite people")


def test_unknown_role_and_bad_input_are_refused(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    email = _email(admin, people["b"])
    _denied(act_as("a"), INVITE, (org, email, "emperor"), match="unknown role")
    for bad in ("nonsense", "a b@example.org", "x@y", "x" * 250 + "@example.org", "ünï@example.org"):
        _denied(act_as("a"), INVITE, (org, bad, "contributor"), match="not a valid email address")
    _denied(act_as("a"), INVITE_NEW, (org, email, "contributor", "x" * 201, None), match="name is not acceptable")
    _denied(act_as("a"), INVITE_NEW, (org, email, "contributor", "tab\there", None), match="name is not acceptable")


# --- every kind of target answers alike ---------------------------------------


def test_every_target_state_creates_an_invitation_row(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    _grant(world, "a", org, "b", "contributor")
    for who in ("b", "d_disabled", "pa1"):  # current member, disabled, vendor staff
        invitation = _invite(world, "a", org, who, "contributor" if who != "b" else "point_person")
        assert _state(admin, invitation)[:3] == (False, False, False)
    # ...but the unactionable ones can never be accepted.
    dup = _invite(world, "a", org, "b", "contributor")  # already holds this role
    _denied(act_as("b"), ACCEPT, (dup,), match="no such invitation")
    assert _state(admin, dup)[:3] == (False, False, False)  # still open: the membership never appeared beside it


# --- accept-time checks --------------------------------------------------------


def test_expired_invitations_cannot_be_accepted_and_are_renewed_in_place(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    invitation = _run(admin, "INSERT INTO org_invitations (org_id, invited_user_id, role, invited_by, invited_at, expires_at) "
                      "VALUES (%s, %s, 'contributor', %s, now() - interval '20 days', now() - interval '6 days') "
                      "RETURNING invitation_id", (org, people["b"], people["a"]))[0]
    _denied(act_as("b"), ACCEPT, (invitation,), match="no such invitation")
    renewed = _run(act_as("a"), INVITE_NEW, (org, _email(admin, people["b"]), "contributor", None, None))
    assert renewed[0] == invitation and renewed[2] is True
    _accept(world, "b", invitation)


def test_inviter_who_lost_authority_leaves_a_dead_invitation(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    mc = _grant(world, "a", org, "c", "point_person")
    invitation = _invite(world, "c", org, "b", "contributor")
    _run(act_as("a"), REVOKE, (mc,))
    _denied(act_as("b"), ACCEPT, (invitation,), match="no such invitation")


def test_suspended_org_or_promoted_invitee_blocks_acceptance(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    inv_b = _invite(world, "a", org, "b", "contributor")
    inv_c = _invite(world, "a", org, "c", "contributor")
    _run(admin, "UPDATE users SET is_platform_admin = true WHERE user_id = %s", (people["c"],))
    _denied(act_as("c"), ACCEPT, (inv_c,), match="no such invitation")
    _run(act_as("pa1"), "UPDATE organizations SET status = 'suspended' WHERE org_id = %s", (org,))
    _denied(act_as("b"), ACCEPT, (inv_b,), match="no such invitation")


def test_revoked_invitation_cannot_be_accepted(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    invitation = _invite(world, "a", org, "b", "contributor")
    _run(act_as("a"), REVOKE_INVITATION, (invitation,))
    assert _state(admin, invitation)[:3] == (False, False, True)
    _denied(act_as("b"), ACCEPT, (invitation,), match="no such invitation")


def test_who_may_revoke_an_invitation(world):
    people, act_as, _, _ = world
    org = _org_with_admin(world)
    _grant(world, "a", org, "c", "point_person")
    by_c = _invite(world, "c", org, "b", "contributor")
    _denied(act_as("b"), REVOKE_INVITATION, (by_c,), match="no such invitation")  # the invitee can only decline
    _denied(act_as("pa1"), REVOKE_INVITATION, (by_c,), match="no such invitation")
    _run(act_as("c"), REVOKE_INVITATION, (by_c,))  # the sender
    by_a = _invite(world, "a", org, "b", "contributor")
    _denied(act_as("c"), REVOKE_INVITATION, (by_a,), match="no such invitation")  # a point person can't revoke an admin's
    _run(act_as("a"), REVOKE_INVITATION, (by_a,))


# --- the membership rule -------------------------------------------------------


def test_forging_the_acceptance_setting_buys_nothing(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    inv_b = _invite(world, "a", org, "b", "contributor")
    # Someone else naming b's invitation cannot use it.
    conn = act_as("c")
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.accepting_invitation', %s, false)", (str(inv_b),))
    _denied(conn, GRANT, (org, people["c"], "contributor"), match="memberships are created by accepting")
    _denied(conn, GRANT, (org, people["b"], "contributor"), match="memberships are created by accepting")
    # b cannot use their own invitation for a different role or org.
    conn = act_as("b")
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.accepting_invitation', %s, false)", (str(inv_b),))
    _denied(conn, GRANT, (org, people["b"], "org_admin"), match="memberships are created by accepting")
    _denied(conn, GRANT, (_new_org(act_as), people["b"], "contributor"), match="memberships are created by accepting")
    # A garbage setting is ignored, not an error of its own.
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.accepting_invitation', 'not-a-uuid', false)")
    _denied(conn, GRANT, (org, people["b"], "contributor"), match="memberships are created by accepting")
    assert _active_memberships(admin, org, people["b"]) == []


def test_matching_setting_used_directly_still_closes_the_invitation(world):
    """Even without calling accept_invitation(), a matching insert cannot
    leave an invitation open beside a granted membership."""
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    invitation = _invite(world, "a", org, "b", "contributor")
    conn = act_as("b")
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.accepting_invitation', %s, true)", (str(invitation),))
        cur.execute(GRANT + " RETURNING granted_by", (org, people["b"], "contributor"))
        assert cur.fetchone()[0] == people["a"]
    conn.commit()
    assert _state(admin, invitation)[:3] == (True, False, False)


def test_changing_the_acting_user_mid_statement_still_closes_the_invitation(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    invitation = _invite(world, "a", org, "b", "contributor")
    conn = act_as("b")
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.accepting_invitation', %s, false)", (str(invitation),))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "WITH ins AS (INSERT INTO org_memberships (org_id, user_id, role) VALUES (%s, %s, 'contributor') RETURNING 1) "
            "SELECT set_config('app.user_id', %s, true) FROM ins", (org, people["b"], str(people["a"])))
    conn.commit()
    # The closing is keyed on the inserted row, so the invitation is closed
    # beside the membership whatever the acting-user setting becomes.
    assert _state(admin, invitation)[:3] == (True, False, False)
    assert _active_memberships(admin, org, people["b"]) == [("contributor", people["a"])]


def test_on_conflict_do_nothing_cannot_close_an_invitation_without_a_membership(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    _grant(world, "a", org, "b", "contributor")
    invitation = _invite(world, "a", org, "b", "contributor")  # duplicates an active role
    conn = act_as("b")
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.accepting_invitation', %s, true)", (str(invitation),))
        cur.execute(GRANT + " ON CONFLICT DO NOTHING", (org, people["b"], "contributor"))
    conn.commit()
    assert _state(admin, invitation)[:3] == (False, False, False)
    assert len(_active_memberships(admin, org, people["b"])) == 1


def _direct_accept_attempt(world, who, invitation, org, role="contributor"):
    """b names a real invitation in the setting and inserts the membership
    itself (not through accept_invitation)."""
    people, act_as, _, _ = world
    conn = act_as(who)
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.accepting_invitation', %s, false)", (str(invitation),))
    conn.commit()
    return conn, (org, people[who], role)


def test_direct_insert_rechecks_everything_the_function_checks(world):
    people, act_as, _, admin = world
    for label in ("inviter demoted", "org suspended", "invitee promoted", "invitee disabled"):
        org = _org_with_admin(world)
        mc = _grant(world, "a", org, "c", "point_person")
        invitation = _invite(world, "c", org, "b", "contributor")
        if label == "inviter demoted":
            _run(act_as("a"), REVOKE, (mc,))
        elif label == "org suspended":
            _run(act_as("pa1"), "UPDATE organizations SET status = 'suspended' WHERE org_id = %s", (org,))
        elif label == "invitee promoted":
            _run(admin, "UPDATE users SET is_platform_admin = true WHERE user_id = %s", (people["b"],))
        else:
            _run(admin, "UPDATE users SET disabled_at = now() WHERE user_id = %s", (people["b"],))
        conn, params = _direct_accept_attempt(world, "b", invitation, org)
        _denied(conn, GRANT, params, match="invitation can no longer be accepted")
        _run(admin, "UPDATE users SET is_platform_admin = false, disabled_at = NULL WHERE user_id = %s", (people["b"],))
    # An expired invitation is simply not matched, so the plain rule answers.
    org = _org_with_admin(world)
    expired = _run(admin, "INSERT INTO org_invitations (org_id, invited_user_id, role, invited_by, invited_at, expires_at) "
                   "VALUES (%s, %s, 'contributor', %s, now() - interval '20 days', now() - interval '6 days') RETURNING invitation_id",
                   (org, people["b"], people["a"]))[0]
    conn, params = _direct_accept_attempt(world, "b", expired, org)
    _denied(conn, GRANT, params, match="memberships are created by accepting an invitation")


def test_multi_row_insert_cannot_skip_closing_an_invitation(world):
    """A single statement whose rows are matched against different settings
    must still close every invitation it uses."""
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    inv_contributor = _invite(world, "a", org, "b", "contributor")
    inv_point = _invite(world, "a", org, "b", "point_person")
    conn = act_as("b")
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.accepting_invitation', %s, false)", (str(inv_contributor),))
        cur.execute(
            "INSERT INTO org_memberships (org_id, user_id, role) VALUES (%s, %s, 'contributor'), "
            "(%s, %s, (SELECT 'point_person' FROM set_config('app.accepting_invitation', %s, true)))",
            (org, people["b"], org, people["b"], str(inv_point)))
    conn.commit()
    assert _state(admin, inv_contributor)[:3] == (True, False, False)
    assert _state(admin, inv_point)[:3] == (True, False, False)
    assert {r[0] for r in _active_memberships(admin, org, people["b"])} == {"contributor", "point_person"}


def test_runtime_cannot_write_invitations_directly(world):
    people, act_as, _, _ = world
    org = _org_with_admin(world)
    _denied(act_as("a"), "INSERT INTO org_invitations (org_id, invited_user_id, role, invited_by) VALUES (%s, %s, 'contributor', %s)",
            (org, people["b"], people["a"]), match="permission denied for table org_invitations")
    invitation = _invite(world, "a", org, "b", "contributor")
    _denied(act_as("b"), "UPDATE org_invitations SET accepted_at = now() WHERE invitation_id = %s", (invitation,),
            match="permission denied for table org_invitations")


# --- new accounts ----------------------------------------------------------------


def test_new_person_needs_an_account_id_then_gets_an_account_and_an_invitation(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    email = _new_address(admin, people)
    conn = act_as("a")
    with pytest.raises(psycopg.Error) as exc:
        with conn.cursor() as cur:
            cur.execute(INVITE_NEW, (org, email, "contributor", None, None))
    assert exc.value.sqlstate == "P0A01"
    conn.rollback()
    sub = str(uuid.uuid4())
    invitation, normalized, created = _run(act_as("a"), INVITE_NEW, (org, email.upper(), "contributor", " New Person ", sub.upper()))
    assert normalized == email and created is True
    with admin.cursor() as cur:
        cur.execute("SELECT user_id, cognito_sub, display_name, is_platform_admin, disabled_at FROM users WHERE email = %s", (email,))
        user_id, stored_sub, name, is_admin, disabled = cur.fetchone()
        cur.execute("SELECT detail->>'source', actor_user_id FROM audit_log WHERE action = 'user.create' AND object_id = %s", (str(user_id),))
        audit = cur.fetchone()
    admin.rollback()
    assert (stored_sub, name, is_admin, disabled) == (sub, "New Person", False, None)
    assert audit == ("database", people["a"])
    # Nothing granted yet; the new person accepts like everyone else.
    assert _active_memberships(admin, org, user_id) == []


def test_account_id_problems_are_refused(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    email = _new_address(admin, people)
    _denied(act_as("a"), INVITE_NEW, (org, email, "contributor", None, "not-a-uuid"), match="invalid account id")
    taken = str(uuid.uuid4())
    _run(admin, "UPDATE users SET cognito_sub = %s WHERE user_id = %s", (taken, people["b"]))
    # An account id that already belongs to someone else cannot be attached to a new address.
    _denied(act_as("a"), INVITE_NEW, (org, email, "contributor", None, taken), match="that account could not be set up")


# --- caps ------------------------------------------------------------------------


def _fill_closed_invitations(admin, org, inviter, invitee, how_many):
    with admin.cursor() as cur:
        for _ in range(how_many):
            cur.execute(
                "INSERT INTO org_invitations (org_id, invited_user_id, role, invited_by, declined_at) "
                "VALUES (%s, %s, 'contributor', %s, now())", (org, invitee, inviter))
    admin.commit()


def test_per_caller_cap_counts_every_case(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    _fill_closed_invitations(admin, org, people["a"], people["b"], 10)
    _denied(act_as("a"), INVITE, (org, _email(admin, people["c"]), "contributor"), match="sent too many invitations")


def test_per_org_cap(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    _fill_closed_invitations(admin, org, people["c"], people["b"], 20)
    _denied(act_as("a"), INVITE, (org, _email(admin, people["c"]), "contributor"), match="too many invitations today for this organization")


def test_caller_cap_boundary(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    _fill_closed_invitations(admin, org, people["a"], people["b"], 9)
    _run(act_as("a"), INVITE, (org, _email(admin, people["c"]), "contributor"))  # the 10th is allowed
    _denied(act_as("a"), INVITE, (org, _email(admin, people["c"]), "point_person"), match="sent too many invitations")


def test_begin_applies_the_same_authorization_and_caps_without_writing(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    BEGIN = "SELECT invite_begin(%s, %s)"
    _run(act_as("a"), BEGIN, (org, "contributor"))
    _denied(act_as("b"), BEGIN, (org, "contributor"), match="not allowed to invite people")
    _denied(act_as("a"), BEGIN, (uuid.uuid4(), "contributor"), match="not allowed to invite people")
    _denied(act_as("a"), BEGIN, (org, "emperor"), match="unknown role")
    _fill_closed_invitations(admin, org, people["a"], people["b"], 10)
    _denied(act_as("a"), BEGIN, (org, "contributor"), match="sent too many invitations")
    with admin.cursor() as cur:
        cur.execute("SELECT count(*) FROM org_invitations WHERE org_id = %s", (org,))
        assert cur.fetchone()[0] == 10  # beginning wrote nothing


def test_begin_holds_the_senders_and_the_orgs_locks_until_the_transaction_ends(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    _grant(world, "a", org, "c", "point_person")
    first = act_as("a")
    with first.cursor() as cur:
        cur.execute("SELECT invite_begin(%s, 'contributor')", (org,))  # transaction left open
    # The same sender, and a different sender in the same org, both wait.
    for who in ("a", "c"):
        other = act_as(who)
        with other.cursor() as cur:
            cur.execute("SET statement_timeout = 400")
            with pytest.raises(psycopg.errors.QueryCanceled):
                cur.execute("SELECT invite_begin(%s, 'contributor')", (org,))
        other.rollback()
    first.rollback()
    _run(act_as("c"), "SELECT invite_begin(%s, 'contributor')", (org,))  # free again


def test_waiting_for_the_locks_is_bounded(world):
    people, act_as, _, _ = world
    org = _org_with_admin(world)
    first = act_as("a")
    with first.cursor() as cur:
        cur.execute("SELECT invite_begin(%s, 'contributor')", (org,))
    second = act_as("a")
    with second.cursor() as cur:
        with pytest.raises(psycopg.errors.LockNotAvailable):
            cur.execute("SELECT invite_begin(%s, 'contributor')", (org,))
    second.rollback()
    first.rollback()


def test_global_cap_protects_the_shared_email_quota(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    for _ in range(3):
        other = _new_org(act_as)
        _fill_closed_invitations(admin, other, people["c"], people["b"], 10)
    _denied(act_as("a"), INVITE, (org, _email(admin, people["c"]), "contributor"), match="invitations are limited right now")


def test_cap_applies_before_the_target_is_looked_at(world):
    """At the cap a brand-new address gets the cap error, not account_needed,
    so the two cases can't be told apart."""
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    _fill_closed_invitations(admin, org, people["a"], people["b"], 10)
    conn = act_as("a")
    with pytest.raises(psycopg.Error) as exc:
        with conn.cursor() as cur:
            cur.execute(INVITE_NEW, (org, _new_address(admin, people), "contributor", None, None))
    assert exc.value.sqlstate == "P0A02"
    conn.rollback()


# --- audit -----------------------------------------------------------------------


def test_invitation_events_are_audited_by_the_database(world):
    people, act_as, _, admin = world
    org = _org_with_admin(world)
    accepted = _invite(world, "a", org, "b", "contributor")
    _accept(world, "b", accepted)
    declined = _invite(world, "a", org, "c", "contributor")
    _run(act_as("c"), DECLINE, (declined,))
    revoked = _invite(world, "a", org, "c", "point_person")
    _run(act_as("a"), REVOKE_INVITATION, (revoked,))
    with admin.cursor() as cur:
        cur.execute(
            "SELECT action, actor_user_id, detail->>'source', detail FROM audit_log "
            "WHERE org_id = %s AND (action LIKE 'invitation.%%' OR (action = 'membership.grant' AND detail ? 'invitation_id')) "
            "ORDER BY audit_id", (org,))
        rows = cur.fetchall()
    admin.rollback()
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("invitation.create", people["a"], "database"),
        ("invitation.accept", people["b"], "database"),
        ("membership.grant", people["b"], "database"),
        ("invitation.create", people["a"], "database"),
        ("invitation.decline", people["c"], "database"),
        ("invitation.create", people["a"], "database"),
        ("invitation.revoke", people["a"], "database"),
    ]
    assert rows[2][3]["invitation_id"] == str(accepted)
    assert rows[6][3]["by_sender"] is True
    assert "account_created" not in rows[0][3]


def test_the_api_cannot_forge_invitation_audit_events(world):
    people, act_as, _, _ = world
    org = _org_with_admin(world)
    _denied(act_as("a"), "SELECT audit_write('invitation.create', 'invitation', 'x', %s, '{}'::jsonb)", (org,),
            match="reserved for the database")
