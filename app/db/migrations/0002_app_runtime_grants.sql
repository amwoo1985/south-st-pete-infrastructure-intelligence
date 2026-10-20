-- The API's runtime role (app_runtime, created by app/db/migrate.py before
-- any migration runs) gets row-level read/write on the application tables
-- and nothing else: no DDL, no ownership, no ability to alter grants.
-- "No CREATE TABLE" relies on PostgreSQL 15+ no longer granting CREATE on
-- schema public to PUBLIC (RDS and local are both 16; tests/db/test_migrate.py
-- asserts it).

GRANT USAGE ON SCHEMA public TO app_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON chunks, document_uploads TO app_runtime;

-- Read-only: the API's startup check (assert_schema_current) reads it.
GRANT SELECT ON schema_migrations TO app_runtime;

-- Tables and sequences created by later migrations (run as this same
-- migrating role) get the same row-level rights automatically. A migration
-- that needs narrower rights for app_runtime (e.g. an insert-only audit log)
-- must REVOKE explicitly; tests/db/test_migrate.py checks every table.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app_runtime;
