-- 001: pgvector schema for Titan Text Embeddings v2 (1024-d), brief §7.1.
-- Applied by ingest.vectorstore.migrations (the migrate Lambda); never edit after it has run.

CREATE EXTENSION IF NOT EXISTS vector;

-- halfvec needs pgvector >= 0.7; iterative index scans (used at query time) need >= 0.8.
DO $$
DECLARE v int[] := string_to_array((SELECT extversion FROM pg_extension WHERE extname = 'vector'), '.')::int[];
BEGIN
  IF v < ARRAY[0, 8, 0] THEN
    RAISE EXCEPTION 'pgvector >= 0.8.0 required, found %', array_to_string(v, '.');
  END IF;
END $$;

-- Database users. Lambdas connect through RDS Proxy with IAM auth (rds_iam on Aurora).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ingest_writer') THEN
    CREATE ROLE ingest_writer LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ingest_reader') THEN
    CREATE ROLE ingest_reader LOGIN;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam') THEN
    GRANT rds_iam TO ingest_writer, ingest_reader;
  END IF;
END $$;

CREATE TABLE chunks_titan2_1024 (
  chunk_id         text PRIMARY KEY,
  article_id       text NOT NULL,
  doc_version      int NOT NULL,
  ordinal          int NOT NULL,
  parent_id        text,
  process_id       text,
  tenant_id        text,
  is_active        boolean NOT NULL DEFAULT false,
  text             text NOT NULL,
  embed_text       text NOT NULL,
  heading_path     text[],
  page_start       int,
  page_end         int,
  token_count      int NOT NULL,
  metadata         jsonb NOT NULL DEFAULT '{}',
  content_tsv      tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  embedding        halfvec(1024) NOT NULL,
  embedding_model  text NOT NULL,
  pipeline_version text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX chunks_titan2_1024_embedding_hnsw ON chunks_titan2_1024
  USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64) WHERE is_active;
CREATE INDEX chunks_titan2_1024_article_version ON chunks_titan2_1024 (article_id, doc_version);
CREATE INDEX chunks_titan2_1024_tsv ON chunks_titan2_1024 USING gin (content_tsv) WHERE is_active;
CREATE INDEX chunks_titan2_1024_tenant ON chunks_titan2_1024 (tenant_id) WHERE is_active;

-- Readers query this view; a model change switches it to another table (brief §7.3).
CREATE OR REPLACE VIEW chunks_active AS SELECT * FROM chunks_titan2_1024 WHERE is_active;

GRANT SELECT, INSERT, UPDATE, DELETE ON chunks_titan2_1024 TO ingest_writer;
GRANT SELECT ON chunks_titan2_1024, chunks_active TO ingest_reader, ingest_writer;
