-- Runs automatically on first container init (empty data dir) via
-- /docker-entrypoint-initdb.d/. Idempotent pre-check: ensures the pgvector
-- extension exists rather than leaving app code to assume it does.
CREATE EXTENSION IF NOT EXISTS vector;
