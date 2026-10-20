-- Moves the pre-grant-app civic-records corpus out of the application's
-- tables into an `archive` schema, keeping every row (DECISIONS #161).
--
-- The existing tables are MOVED (ALTER TABLE ... SET SCHEMA), not copied:
-- the rows, every column (including any this repo doesn't know about),
-- indexes and constraints travel with the table, atomically, under an
-- ACCESS EXCLUSIVE lock. There is no copy-then-delete window in which a
-- concurrent write from the still-running old API could be lost. Fresh,
-- empty application tables are then created in `public`.
--
-- app_runtime has no USAGE on `archive`, so the API cannot read it.

CREATE SCHEMA IF NOT EXISTS archive;

ALTER TABLE public.chunks SET SCHEMA archive;
ALTER TABLE archive.chunks RENAME TO civic_chunks;
ALTER TABLE public.document_uploads SET SCHEMA archive;

-- Legacy tables the grant app never uses.
ALTER TABLE IF EXISTS public.crawl_runs SET SCHEMA archive;
ALTER TABLE IF EXISTS public.granicus_transcription_jobs SET SCHEMA archive;

-- The moved tables carried 0002's app_runtime grants with them; remove
-- every path from the runtime role to archived data.
REVOKE ALL ON SCHEMA archive FROM app_runtime;
REVOKE ALL ON ALL TABLES IN SCHEMA archive FROM app_runtime;

-- Fresh application tables (same definitions as 0001).
CREATE TABLE public.chunks (
    chunk_id TEXT PRIMARY KEY,
    doc_type TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    section_label TEXT,
    source_url TEXT NOT NULL,
    published_date DATE,
    retrieval_timestamp TIMESTAMPTZ NOT NULL,
    start_seconds DOUBLE PRECISION,
    end_seconds DOUBLE PRECISION,
    embedding VECTOR(1536),
    embedding_model TEXT,
    embedded_at TIMESTAMPTZ
);

CREATE INDEX chunks_embedding_hnsw_idx
    ON public.chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE public.document_uploads (
    file_hash TEXT PRIMARY KEY,
    original_filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('processing', 'completed', 'failed')),
    failure_reason TEXT,
    chunk_count INTEGER,
    uploaded_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ
);

-- Explicit, in addition to 0002's default privileges, so the grants don't
-- depend on which role happens to run this migration.
GRANT SELECT, INSERT, UPDATE, DELETE ON public.chunks, public.document_uploads TO app_runtime;
