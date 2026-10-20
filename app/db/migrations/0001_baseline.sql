-- Baseline: the schema as it existed before ordered migrations (DECISIONS
-- #160). Every statement is idempotent so an existing database (production
-- RDS, a dev volume) adopts this as version 0001 without changes, and a fresh
-- database gets the full schema.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    doc_type TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    content_hash TEXT,
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

CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);

-- Databases created before content_hash existed get the column here; the
-- NOT NULL lock only applies once no NULL rows remain (DECISIONS #154).
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS content_hash TEXT;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'chunks'
          AND column_name = 'content_hash' AND is_nullable = 'YES'
    ) AND NOT EXISTS (
        SELECT 1 FROM chunks WHERE content_hash IS NULL
    ) THEN
        ALTER TABLE chunks ALTER COLUMN content_hash SET NOT NULL;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS document_uploads (
    file_hash TEXT PRIMARY KEY,
    original_filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('processing', 'completed', 'failed')),
    failure_reason TEXT,
    chunk_count INTEGER,
    uploaded_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ
);
