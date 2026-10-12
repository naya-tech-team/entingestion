-- 002: a second vector table for Titan Text Embeddings v2 at 512 dimensions (brief §7.3).
-- Filled by reindex (from_stage EMBED); goes live when the chunks_active view switches to it.

CREATE TABLE chunks_compact (LIKE chunks_primary INCLUDING DEFAULTS INCLUDING GENERATED);
ALTER TABLE chunks_compact ALTER COLUMN embedding TYPE halfvec(512);
ALTER TABLE chunks_compact ADD PRIMARY KEY (chunk_id);

CREATE INDEX chunks_compact_embedding_hnsw ON chunks_compact
  USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64) WHERE is_active;
CREATE INDEX chunks_compact_article_version ON chunks_compact (article_id, doc_version);
CREATE INDEX chunks_compact_tsv ON chunks_compact USING gin (content_tsv) WHERE is_active;
CREATE INDEX chunks_compact_tenant ON chunks_compact (tenant_id) WHERE is_active;

GRANT SELECT, INSERT, UPDATE, DELETE ON chunks_compact TO ingest_writer;
GRANT SELECT ON chunks_compact TO ingest_reader, ingest_writer;
