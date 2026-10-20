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
        # Legacy civic-records tables (pre-#158), unused by the API and not
        # granted. The #159 purge migration drops them; remove these two
        # entries in that commit.
        "crawl_runs": set(),
        "granicus_transcription_jobs": set(),
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
