"""The operator's command-line tool for setting an organization up
(DECISIONS #166, #171, #174). It is the ONLY way a first or recovery org admin
is seated, and the only way a platform admin is created.

    python -m app.admin create-user --cognito-sub <uuid> --email <addr> [--display-name N] [--platform-admin [--as <operator email>]] --reason "..."
    python -m app.admin create-org --legal-name "..." --as <operator email> --reason "..."
    python -m app.admin grant-org-admin --org <org id> --user <email> --as <operator email> --reason "..."

Add ``--apply`` to save. Without it each command does everything, including the
database's own rules, and then rolls back ("dry run"), so a refusal shows up
before anything is committed.

How it works, plainly:
- It connects as the database owner (the migration login), which the database
  recognizes as the privileged role. That role may do these setup changes only
  when it names a reason: ``--reason`` is set as ``app.system_job`` and is
  written into the audit log. Changes that record a person (who created an
  org, who granted a membership) also need ``app.user_id`` set to the real
  operator: ``--as`` names a platform admin, who is set as the acting user.
  A user row created here carries the reason only (there is no acting user),
  unless it makes a platform admin while an ACTIVE one exists: then ``--as``
  (an active platform admin) is required and is recorded as the actor. With no
  active platform admin (the first one, or after the last was disabled) no
  operator is needed. That is a bootstrap convenience, not a boundary: anyone who
  can run this task already holds the database owner login, and two simultaneous
  runs could both create one.
- ``--as`` is a typed email, not a login: it attributes the change to that
  person, but nothing here proves they ran it. The real caller of the task is
  in AWS CloudTrail, and ``infra/run_admin.sh`` appends the caller's AWS
  identity to the reason.
- The privileged path skips the database's invitation rules, so the rules that
  matter are checked HERE and are covered by tests: the operator must be an
  active platform admin; an org admin can't be a platform admin (vendor staff
  reach customer data only through consented support access); the org must be
  active; the person must have an active account.
- Every command is check-then-act, so running it twice is a no-op, not a
  duplicate. Output is ids, names and emails only. Nothing here reads or
  prints a secret; the database password arrives in the process environment.
- Cognito is not touched: the account must already exist in the user pool, and
  its ``sub`` (shown in the Cognito console under the user) is passed in.

Exit status: 0 done (or nothing to do), 1 refused or failed, 2 bad usage.
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass
from uuid import UUID

import psycopg

from app.db.connection import get_migration_connection

_EMAIL_RE = re.compile(r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class AdminError(Exception):
    """A refusal with a message safe to show the operator."""


@dataclass(frozen=True)
class Result:
    message: str
    changed: bool


def _email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not _EMAIL_RE.match(email):
        raise AdminError(f"not a plain email address: {value!r}")
    return email


def _plain(value: str) -> bool:
    """No control or invisible formatting characters (including bidi overrides)."""
    return not any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in value)


def _reason(value: str) -> str:
    reason = value.strip()
    if not reason or len(reason) > 500 or not _plain(reason):
        raise AdminError("--reason must be a short sentence (1-300 characters when run through run_admin.sh, no control characters)")
    return reason


def _begin(conn, reason: str, operator_id: UUID | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.system_job', %s, false)", (reason,))
        cur.execute("SELECT set_config('app.user_id', %s, false)", (str(operator_id) if operator_id else "",))


def _operator(conn, email: str) -> UUID:
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM users WHERE email = %s AND disabled_at IS NULL AND is_platform_admin", (email,))
        row = cur.fetchone()
    if row is None:
        raise AdminError(f"--as {email} is not an active platform admin")
    return row[0]


def create_user(conn, *, cognito_sub: str, email: str, display_name: str | None, platform_admin: bool, reason: str,
                operator_email: str | None = None) -> Result:
    sub = cognito_sub.strip().lower()
    if not _UUID_RE.match(sub):
        raise AdminError("--cognito-sub must be the user's UUID from the Cognito console")
    email = _email(email)
    if display_name is not None and (len(display_name) > 200 or not _plain(display_name)):
        raise AdminError("--display-name is too long or has control characters")
    with conn.cursor() as cur:  # a repeat of an identical, already-applied command is a no-op
        cur.execute("SELECT user_id FROM users WHERE email = %s AND cognito_sub = %s AND is_platform_admin = %s",
                    (email, sub, platform_admin))
        same = cur.fetchone()
    if same:
        return Result(f"already exists: {email} (user {same[0]}); nothing to do", False)
    operator = None
    if platform_admin:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM users WHERE is_platform_admin AND disabled_at IS NULL")
            have_one = cur.fetchone()[0] > 0
        if have_one and operator_email is None:
            raise AdminError("a platform admin already exists, so creating another needs --as <an active platform admin>")
        if operator_email is not None:
            operator = _operator(conn, _email(operator_email))
    _begin(conn, _reason(reason), operator)
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, cognito_sub, email, is_platform_admin FROM users WHERE email = %s OR cognito_sub = %s",
                    (email, sub))
        rows = cur.fetchall()
        if rows:
            if len(rows) == 1 and rows[0][1] == sub and rows[0][2] == email and rows[0][3] == platform_admin:
                return Result(f"already exists: {email} (user {rows[0][0]}); nothing to do", False)
            raise AdminError("that email or Cognito sub already belongs to a different account; nothing changed")
        cur.execute(
            "INSERT INTO users (cognito_sub, email, display_name, is_platform_admin) VALUES (%s, %s, %s, %s) RETURNING user_id",
            (sub, email, display_name.strip() if display_name and display_name.strip() else None, platform_admin),
        )
        user_id = cur.fetchone()[0]
    kind = "platform admin" if platform_admin else "user"
    return Result(f"created {kind} {email} (user {user_id})", True)


def create_org(conn, *, legal_name: str, operator_email: str, reason: str) -> Result:
    name = legal_name.strip()
    if not name or len(name) > 300 or not _plain(name):
        raise AdminError("--legal-name must be 1-300 characters without control characters")
    operator = _operator(conn, _email(operator_email))
    _begin(conn, _reason(reason), operator)
    with conn.cursor() as cur:
        # Two runs at once for the same name can't both pass the check below.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("org-name:" + name.lower(),))
        cur.execute("SELECT org_id, legal_name FROM organizations WHERE lower(btrim(legal_name)) = lower(%s)", (name,))
        rows = cur.fetchall()
        if rows:
            if len(rows) == 1:
                return Result(f"already exists: {rows[0][1]!r} (org {rows[0][0]}); nothing to do", False)
            raise AdminError("several organizations already have that name; nothing changed")
        cur.execute("INSERT INTO organizations (legal_name) VALUES (%s) RETURNING org_id", (name,))
        org_id = cur.fetchone()[0]
    return Result(f"created organization {name!r} (org {org_id})", True)


def grant_org_admin(conn, *, org_id: str, user_email: str, operator_email: str, reason: str) -> Result:
    org_id = org_id.strip().lower()
    if not _UUID_RE.match(org_id):
        raise AdminError("--org must be the organization's id (a UUID)")
    user_email = _email(user_email)
    operator = _operator(conn, _email(operator_email))
    _begin(conn, _reason(reason), operator)
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM organizations WHERE org_id = %s FOR SHARE", (org_id,))
        org = cur.fetchone()
        if org is None:
            raise AdminError("no such organization")
        if org[0] != "active":
            raise AdminError(f"the organization is {org[0]}, not active")
        cur.execute("SELECT user_id, disabled_at IS NULL, is_platform_admin FROM users WHERE email = %s", (user_email,))
        person = cur.fetchone()
        if person is None or not person[1]:
            raise AdminError(f"{user_email} has no active account (run create-user first)")
        if person[2]:
            raise AdminError(f"{user_email} is a platform admin; vendor staff reach customer data only through "
                             "consented support access, never as members (use a separate login)")
        cur.execute("SELECT 1 FROM org_memberships WHERE org_id = %s AND user_id = %s AND role = 'org_admin' AND revoked_at IS NULL",
                    (org_id, person[0]))
        if cur.fetchone():
            return Result(f"{user_email} is already an org admin there; nothing to do", False)
        cur.execute("INSERT INTO org_memberships (org_id, user_id, role) VALUES (%s, %s, 'org_admin') RETURNING membership_id",
                    (org_id, person[0]))
        membership = cur.fetchone()[0]
    return Result(f"{user_email} is now an org admin of {org_id} (membership {membership})", True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.admin", description=__doc__.split("\n\n")[0], allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name):
        return sub.add_parser(name, allow_abbrev=False)

    def common(p, needs_operator):
        p.add_argument("--reason", required=True, help="why; recorded in the audit log")
        p.add_argument("--apply", action="store_true", help="save the change (default is a dry run)")
        if needs_operator:
            p.add_argument("--as", dest="operator", required=True, help="email of the acting platform admin")

    p = add("create-user")
    p.add_argument("--cognito-sub", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--display-name")
    p.add_argument("--platform-admin", action="store_true")
    p.add_argument("--as", dest="operator", help="required when making a platform admin and one already exists")
    common(p, False)

    p = add("create-org")
    p.add_argument("--legal-name", required=True)
    common(p, True)

    p = add("grant-org-admin")
    p.add_argument("--org", required=True)
    p.add_argument("--user", required=True)
    common(p, True)
    return parser


def main(argv: list[str], connect=get_migration_connection) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        conn = connect()
    except (psycopg.Error, RuntimeError) as exc:
        print(f"could not connect to the database: {type(exc).__name__}", file=sys.stderr)
        return 1
    committed = False
    try:
        if args.command == "create-user":
            result = create_user(conn, cognito_sub=args.cognito_sub, email=args.email, display_name=args.display_name,
                                 platform_admin=args.platform_admin, reason=args.reason, operator_email=args.operator)
        elif args.command == "create-org":
            result = create_org(conn, legal_name=args.legal_name, operator_email=args.operator, reason=args.reason)
        else:
            result = grant_org_admin(conn, org_id=args.org, user_email=args.user, operator_email=args.operator,
                                     reason=args.reason)
        if args.apply and result.changed:
            conn.commit()
            committed = True
            print(result.message)
        else:
            conn.rollback()
            if result.changed:
                print(f"DRY RUN, nothing saved. It would have: {result.message}\nRe-run with --apply to save.")
            else:
                print(result.message)
        return 0
    except AdminError as exc:
        conn.rollback()
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        conn.rollback()
        sentence = (exc.diag.message_primary or type(exc).__name__)
        print(f"the database refused: {sentence}", file=sys.stderr)
        return 1
    except Exception as exc:  # anything unexpected: never a traceback with details
        conn.rollback()
        print(f"failed unexpectedly ({type(exc).__name__}); "
              + ("the change WAS saved before the failure" if committed else "nothing was saved"), file=sys.stderr)
        return 1
    finally:
        conn.close()
