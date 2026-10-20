"""Ordered schema migrations (DECISIONS #160).

Numbered SQL files in app/db/migrations/ (``NNNN_name.sql``) are applied in
order, each in its own transaction, and recorded in ``schema_migrations``
with a checksum. An applied file that has since been edited fails loud
instead of silently drifting. The API never runs DDL itself: at startup it
only checks that the schema is current (``assert_schema_current``) and
refuses to boot otherwise.

Each migration runs inside one transaction, so a migration file must not
contain anything that can't run in a transaction block: no
``CREATE INDEX CONCURRENTLY``, ``VACUUM`` or explicit ``COMMIT``. Each
session sets ``lock_timeout`` so a migration that can't get its table lock
fails fast instead of queueing behind the live API's queries.

Runs as the table-owning role (``get_migration_connection``). Before any
migration, ensures the API's limited runtime role ``app_runtime`` exists.
If ``APP_RUNTIME_DB_PASSWORD`` is set, the role can log in with it;
otherwise it exists as NOLOGIN so grants still apply.

Usage:
    python -m app.db.migrate up       # apply pending migrations
    python -m app.db.migrate status   # list applied / pending, change nothing
    python -m app.db.migrate report   # read-only summary of data-table contents
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql

from app.db.connection import get_migration_connection

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
RUNTIME_ROLE = "app_runtime"
RUNTIME_PASSWORD_ENV_VAR = "APP_RUNTIME_DB_PASSWORD"

# Arbitrary fixed key: serializes concurrent `up` runs (two deploys at once)
# on one database so neither applies a migration twice.
_ADVISORY_LOCK_KEY = 7_160_001

_CREATE_MIGRATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str  # the NNNN_name stem
    sql_text: str
    checksum: str


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    files = sorted(directory.glob("*.sql"))
    migrations = []
    seen_numbers: set[str] = set()
    for path in files:
        number = path.stem.split("_", 1)[0]
        if not (len(number) == 4 and number.isdigit()):
            raise MigrationError(f"{path.name}: migration files must start with a 4-digit number")
        if number in seen_numbers:
            raise MigrationError(f"{path.name}: duplicate migration number {number}")
        seen_numbers.add(number)
        # Normalize line endings before hashing: git's autocrlf can check a
        # file out as CRLF on one machine and LF in the image, and that must
        # not read as "edited after being applied".
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        migrations.append(
            Migration(
                version=path.stem,
                sql_text=text,
                checksum=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        )
    return migrations


def _applied(conn: psycopg.Connection) -> dict[str, str]:
    """version -> checksum for every recorded migration; empty if the
    tracking table doesn't exist yet."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.schema_migrations')")
        if cur.fetchone()[0] is None:
            return {}
        cur.execute("SELECT version, checksum FROM schema_migrations")
        return dict(cur.fetchall())


def _check_applied_unchanged(applied: dict[str, str], migrations: list[Migration]) -> None:
    known = {m.version: m for m in migrations}
    for version, checksum in applied.items():
        m = known.get(version)
        if m is None:
            raise MigrationError(
                f"Database has migration {version} recorded but no such file exists — "
                "is this an older build than the database?"
            )
        if m.checksum != checksum:
            raise MigrationError(
                f"Migration {version} was edited after being applied (checksum mismatch). "
                "Add a new migration instead of changing an applied one."
            )


def pending(conn: psycopg.Connection, migrations: list[Migration] | None = None) -> list[Migration]:
    migrations = load_migrations() if migrations is None else migrations
    applied = _applied(conn)
    _check_applied_unchanged(applied, migrations)
    return [m for m in migrations if m.version not in applied]


def ensure_runtime_role(conn: psycopg.Connection, password: str | None) -> None:
    """Creates app_runtime if missing. With a password it can log in;
    without one it's NOLOGIN (grants still apply, nothing can connect as
    it). The password is SCRAM-hashed client-side by libpq before it's sent,
    so the plaintext never appears in the statement text, and therefore
    never in a Postgres log (log_statement=ddl, or an error's STATEMENT
    line)."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (RUNTIME_ROLE,))
            exists = cur.fetchone() is not None
            role = sql.Identifier(RUNTIME_ROLE)
            if not exists:
                cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(role))
            if password:
                verifier = conn.pgconn.encrypt_password(
                    password.encode("utf-8"), RUNTIME_ROLE.encode("utf-8"), b"scram-sha-256"
                ).decode("ascii")
                cur.execute(
                    sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(role, sql.Literal(verifier))
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def up(conn: psycopg.Connection, migrations: list[Migration] | None = None) -> list[str]:
    """Applies every pending migration in order; returns the versions applied."""
    migrations = load_migrations() if migrations is None else migrations
    with conn.cursor() as cur:
        # Advisory lock first, with no timeout: a second concurrent `up` waits
        # its turn. lock_timeout then applies only to the migrations' table
        # locks against the live API.
        cur.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
        cur.execute("SET lock_timeout = '5s'")
    conn.commit()
    try:
        with conn.cursor() as cur:
            cur.execute(_CREATE_MIGRATIONS_TABLE_SQL)
        conn.commit()
        ensure_runtime_role(conn, os.environ.get(RUNTIME_PASSWORD_ENV_VAR) or None)
        applied_now = []
        for m in pending(conn, migrations):
            try:
                with conn.cursor() as cur:
                    cur.execute(m.sql_text)
                    cur.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                        (m.version, m.checksum),
                    )
                conn.commit()
            except Exception as exc:
                conn.rollback()
                raise MigrationError(f"Migration {m.version} failed and was rolled back: {exc}") from exc
            applied_now.append(m.version)
        return applied_now
    finally:
        # Clear any aborted transaction first, so the unlock can run and the
        # original error (not InFailedSqlTransaction) is what propagates.
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
        conn.commit()


def assert_schema_current(conn: psycopg.Connection) -> None:
    """Startup guard for the API: raises if any migration is pending or an
    applied one was edited. Read-only."""
    todo = pending(conn)
    conn.rollback()  # end the read transaction; leaves the connection clean
    if todo:
        names = ", ".join(m.version for m in todo)
        raise MigrationError(
            f"Database schema is behind this build (pending: {names}). "
            "Run `python -m app.db.migrate up` (deployed: infra/run_migration.sh up) first."
        )


def report(conn: psycopg.Connection) -> list[str]:
    """Read-only summary of what's in the data tables, live and archived. Returns printable lines."""
    lines = []
    with conn.cursor() as cur:
        for schema, table in (
            ("public", "chunks"),
            ("public", "document_uploads"),
            ("public", "crawl_runs"),
            ("public", "granicus_transcription_jobs"),
            ("archive", "civic_chunks"),
            ("archive", "document_uploads"),
            ("archive", "crawl_runs"),
            ("archive", "granicus_transcription_jobs"),
        ):
            cur.execute("SELECT to_regclass(%s)", (f"{schema}.{table}",))
            if cur.fetchone()[0] is None:
                if schema == "public":
                    lines.append(f"{table}: (table does not exist)")
                continue
            cur.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table)))
            label = table if schema == "public" else f"{schema}.{table}"
            lines.append(f"{label}: {cur.fetchone()[0]} rows")
        if any(line.startswith("chunks:") and "does not exist" not in line for line in lines):
            cur.execute("SELECT doc_type, count(*) FROM chunks GROUP BY doc_type ORDER BY doc_type")
            for doc_type, n in cur.fetchall():
                lines.append(f"  chunks doc_type={doc_type}: {n}")
        if any(line.startswith("document_uploads:") and "does not exist" not in line for line in lines):
            cur.execute(
                "SELECT original_filename, status, chunk_count, uploaded_at "
                "FROM document_uploads ORDER BY uploaded_at"
            )
            for name, status, n, at in cur.fetchall():
                lines.append(f"  upload: {name!r} status={status} chunks={n} uploaded_at={at.isoformat(timespec='minutes')}")
    conn.rollback()
    return lines


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("up", "status", "report"):
        print(__doc__.split("Usage:", 1)[1], file=sys.stderr)
        return 2
    command = argv[1]
    conn = get_migration_connection()
    try:
        if command == "up":
            applied = up(conn)
            print(f"migrate up: applied {len(applied)}: {', '.join(applied) or '(none pending)'}")
        elif command == "status":
            migrations = load_migrations()
            todo = {m.version for m in pending(conn, migrations)}
            for m in migrations:
                print(f"{'pending' if m.version in todo else 'applied'}  {m.version}")
        else:
            for line in report(conn):
                print(line)
    except MigrationError as exc:
        print(f"migrate {command}: FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
