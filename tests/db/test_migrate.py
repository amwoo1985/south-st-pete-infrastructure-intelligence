"""Tests for app/db/migrate.py against the real local Postgres (the session
fixture in tests/conftest.py has already run `up`)."""

from __future__ import annotations

import os
import secrets

import psycopg
import pytest

from app.db import migrate
from app.db.connection import get_migration_connection


@pytest.fixture
def mconn():
    conn = get_migration_connection()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


# --- file loading (no DB) ---------------------------------------------------


def test_load_rejects_unnumbered_file(tmp_path):
    (tmp_path / "baseline.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(migrate.MigrationError, match="4-digit"):
        migrate.load_migrations(tmp_path)


def test_load_rejects_duplicate_number(tmp_path):
    (tmp_path / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0001_b.sql").write_text("SELECT 2;", encoding="utf-8")
    with pytest.raises(migrate.MigrationError, match="duplicate"):
        migrate.load_migrations(tmp_path)


def test_load_orders_by_number(tmp_path):
    (tmp_path / "0002_b.sql").write_text("SELECT 2;", encoding="utf-8")
    (tmp_path / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    assert [m.version for m in migrate.load_migrations(tmp_path)] == ["0001_a", "0002_b"]


def test_edited_applied_migration_fails_loud():
    m = migrate.Migration(version="0001_x", sql_text="SELECT 1;", checksum="new")
    with pytest.raises(migrate.MigrationError, match="edited after being applied"):
        migrate._check_applied_unchanged({"0001_x": "old"}, [m])


def test_recorded_migration_missing_from_build_fails_loud():
    with pytest.raises(migrate.MigrationError, match="no such file"):
        migrate._check_applied_unchanged({"0099_future": "abc"}, [])


# --- against the real DB ----------------------------------------------------


def test_real_migrations_all_applied_and_rerun_is_noop(mconn):
    assert migrate.pending(mconn) == []
    assert migrate.up(mconn) == []


def test_assert_schema_current_passes_when_current(mconn):
    migrate.assert_schema_current(mconn)


def test_pending_detects_unapplied_migration(mconn):
    extra = migrate.Migration(version="9999_not_applied", sql_text="SELECT 1;", checksum="x")
    todo = migrate.pending(mconn, migrate.load_migrations() + [extra])
    assert [m.version for m in todo] == ["9999_not_applied"]


def test_report_is_read_only_summary(mconn):
    lines = migrate.report(mconn)
    assert any(line.startswith("chunks:") for line in lines)
    assert any(line.startswith("document_uploads:") for line in lines)


@pytest.fixture
def runtime(mconn):
    """A real connection as app_runtime — the login the deployed API uses."""
    password = secrets.token_urlsafe(24)
    migrate.ensure_runtime_role(mconn, password)
    conn = psycopg.connect(
        host=os.environ.get("LOCAL_DB_HOST", "127.0.0.1"),
        port=os.environ.get("LOCAL_DB_PORT", "5432"),
        user=migrate.RUNTIME_ROLE,
        password=password,
        dbname=os.environ["LOCAL_DB_NAME"],
    )
    try:
        yield conn
    finally:
        conn.close()


def test_api_startup_check_works_as_runtime_role(runtime):
    migrate.assert_schema_current(runtime)


def test_runtime_password_is_stored_scram_hashed(mconn, runtime):
    with mconn.cursor() as cur:
        cur.execute("SELECT rolpassword FROM pg_authid WHERE rolname = %s", (migrate.RUNTIME_ROLE,))
        assert cur.fetchone()[0].startswith("SCRAM-SHA-256$")


def test_every_table_grants_runtime_role_row_access(mconn):
    """Catches a future migration whose table the API can't use (default
    privileges cover tables created by the migrating role; anything narrower
    must be an explicit, listed exception)."""
    expected_exceptions = {
        "schema_migrations": {"SELECT"},
        "users": {"SELECT"},
        # 0005: table-level rights only; revocation/rename go through
        # column-level UPDATE grants, and audit_log is written only by
        # SECURITY DEFINER functions (DECISIONS #166).
        "organizations": {"SELECT", "INSERT"},
        "org_memberships": {"SELECT", "INSERT"},
        "support_access_grants": {"SELECT", "INSERT"},
        "org_invitations": {"SELECT"},
        "audit_log": set(),
    }
    with mconn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        tables = [row[0] for row in cur.fetchall()]
        for table in tables:
            granted = set()
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                cur.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (migrate.RUNTIME_ROLE, f"public.{table}", priv),
                )
                if cur.fetchone()[0]:
                    granted.add(priv)
            expected = expected_exceptions.get(table, {"SELECT", "INSERT", "UPDATE", "DELETE"})
            assert granted == expected, f"{table}: app_runtime has {sorted(granted)}, expected {sorted(expected)}"


def test_runtime_role_can_use_rows_but_not_change_schema(runtime):
    try:
        with runtime.cursor() as cur:
            cur.execute("SELECT count(*) FROM chunks")
            cur.execute(
                "INSERT INTO document_uploads (file_hash, original_filename, content_type, status, uploaded_at) "
                "VALUES (%s, 'x.txt', 'text/plain', 'processing', now())",
                ("test-runtime-role-" + secrets.token_hex(8),),
            )
        runtime.rollback()
        for ddl in (
            "CREATE TABLE runtime_should_not_create (id int)",
            "ALTER TABLE chunks ADD COLUMN runtime_should_not_add int",
            "DROP TABLE document_uploads",
            "INSERT INTO schema_migrations (version, checksum) VALUES ('x', 'y')",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with runtime.cursor() as cur:
                    cur.execute(ddl)
            runtime.rollback()
    finally:
        runtime.close()


def test_checksum_ignores_crlf_vs_lf(tmp_path):
    lf, crlf = tmp_path / "lf", tmp_path / "crlf"
    lf.mkdir()
    crlf.mkdir()
    (lf / "0001_a.sql").write_bytes(b"SELECT 1;\nSELECT 2;\n")
    (crlf / "0001_a.sql").write_bytes(b"SELECT 1;\r\nSELECT 2;\r\n")
    assert migrate.load_migrations(lf)[0].checksum == migrate.load_migrations(crlf)[0].checksum


def test_runtime_role_has_no_access_to_archive(mconn):
    with mconn.cursor() as cur:
        cur.execute("SELECT has_schema_privilege(%s, 'archive', 'USAGE')", (migrate.RUNTIME_ROLE,))
        assert cur.fetchone()[0] is False


@pytest.fixture
def scratch_db_env(monkeypatch):
    """A throwaway database for migration tests that need a known
    starting state; dropped afterwards."""
    name = "migrate_test_" + secrets.token_hex(4)
    admin = get_migration_connection()
    admin.autocommit = True
    try:
        admin.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(name)))
        monkeypatch.setenv("LOCAL_DB_NAME", name)
        yield name
    finally:
        admin.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(psycopg.sql.Identifier(name)))
        admin.close()


def _rows_as_text(cur, table):
    cur.execute(psycopg.sql.SQL("SELECT t::text FROM {} t ORDER BY 1").format(psycopg.sql.SQL(table)))
    return [row[0] for row in cur.fetchall()]


def test_archive_migration_moves_every_row_and_loses_none(scratch_db_env):
    all_migrations = migrate.load_migrations()
    before_archive = [m for m in all_migrations if m.version < "0003"]
    conn = get_migration_connection()
    try:
        migrate.up(conn, before_archive)
        with conn.cursor() as cur:
            # A column this repo has never heard of (e.g. hand-added in prod)
            # must survive the move too.
            cur.execute("ALTER TABLE chunks ADD COLUMN prod_only_note TEXT")
            cur.execute("CREATE TABLE crawl_runs (id int)")
            cur.execute("INSERT INTO crawl_runs VALUES (1), (2)")
            for i in range(3):
                cur.execute(
                    "INSERT INTO chunks (chunk_id, doc_type, chunk_text, content_hash, source_url, "
                    "retrieval_timestamp, embedding, prod_only_note) "
                    "VALUES (%s, 'legistar', %s, 'h', 'https://x', now(), %s, %s)",
                    (f"c{i}", f"text {i}", "[" + ",".join([f"0.{i + 1}"] * 1536) + "]", f"note {i}"),
                )
            cur.execute(
                "INSERT INTO document_uploads (file_hash, original_filename, content_type, status, uploaded_at) "
                "VALUES ('f1', 'cba_draft.pdf', 'application/pdf', 'completed', now())"
            )
            chunks_before = _rows_as_text(cur, "public.chunks")
            uploads_before = _rows_as_text(cur, "public.document_uploads")
            crawl_before = _rows_as_text(cur, "public.crawl_runs")
        conn.commit()

        through_archive = [m for m in all_migrations if m.version <= "0003_archive_civic_corpus"]
        assert migrate.up(conn, through_archive) == ["0003_archive_civic_corpus"]

        with conn.cursor() as cur:
            # Every row, every column (vector and prod-only column included),
            # byte-for-byte.
            assert _rows_as_text(cur, "archive.civic_chunks") == chunks_before
            assert _rows_as_text(cur, "archive.document_uploads") == uploads_before
            assert _rows_as_text(cur, "archive.crawl_runs") == crawl_before
            # Fresh, empty, usable application tables.
            cur.execute("SELECT count(*) FROM public.chunks")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM public.document_uploads")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT to_regclass('public.crawl_runs')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.chunks_embedding_hnsw_idx')")
            assert cur.fetchone()[0] is not None
            # Runtime role: full row access to the new tables, none to archive.
            for table in ("public.chunks", "public.document_uploads"):
                cur.execute("SELECT has_table_privilege(%s, %s, 'INSERT')", (migrate.RUNTIME_ROLE, table))
                assert cur.fetchone()[0] is True
            cur.execute("SELECT has_schema_privilege(%s, 'archive', 'USAGE')", (migrate.RUNTIME_ROLE,))
            assert cur.fetchone()[0] is False
        conn.rollback()
    finally:
        conn.close()


def test_runtime_role_cannot_create_promote_or_reenable_users(runtime):
    for statement in (
        "INSERT INTO users (cognito_sub, email) VALUES ('sub-escalate', 'x@example.org')",
        "UPDATE users SET is_platform_admin = true",
        "UPDATE users SET disabled_at = NULL",
        "DELETE FROM users",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with runtime.cursor() as cur:
                cur.execute(statement)
        runtime.rollback()
    with runtime.cursor() as cur:
        cur.execute("SELECT count(*) FROM users")
    runtime.rollback()


def test_users_email_is_unique_case_insensitively(mconn):
    from tests.conftest import as_system_job

    as_system_job(mconn)
    with mconn.cursor() as cur:
        cur.execute("INSERT INTO users (cognito_sub, email) VALUES ('sub-a-dup', 'erica@example.org')")
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute("INSERT INTO users (cognito_sub, email) VALUES ('sub-b-dup', 'Erica@Example.org')")
    mconn.rollback()
    with mconn.cursor() as cur:
        cur.execute("INSERT INTO users (cognito_sub, email) VALUES ('sub-a-dup', 'erica@example.org')")
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO users (cognito_sub, email) VALUES ('sub-b-dup', 'erica@example.org')")
    mconn.rollback()


def test_privileged_allowlist_has_migration_role_and_not_runtime(mconn):
    with mconn.cursor() as cur:
        cur.execute(
            "SELECT r.rolname FROM pg_auth_members am "
            "JOIN pg_roles g ON g.oid = am.roleid JOIN pg_roles r ON r.oid = am.member "
            "WHERE g.rolname = 'app_privileged'"
        )
        members = {row[0] for row in cur.fetchall()}
        cur.execute("SELECT current_user")
        me = cur.fetchone()[0]
        cur.execute("SELECT pg_has_role('app_runtime', 'app_privileged', 'MEMBER')")
        runtime_member = cur.fetchone()[0]
    mconn.rollback()
    assert me in members
    assert "app_runtime" not in members and runtime_member is False
