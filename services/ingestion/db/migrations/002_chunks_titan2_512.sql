-- 002: a second vector table for Titan Text Embeddings v2 at 512 dimensions (brief §7.3).
-- Filled by reindex (from_stage EMBED); goes live when the chunks_active view switches to it.

CREATE TABLE chunks_titan2_512 (LIKE chunks_titan2_1024 INCLUDING DEFAULTS INCLUDING GENERATED);
ALTER TABLE chunks_titan2_512 ALTER COLUMN embedding TYPE halfvec(512);
ALTER TABLE chunks_titan2_512 ADD PRIMARY KEY (chunk_id);

CREATE INDEX chunks_titan2_512_embedding_hnsw ON chunks_titan2_512
  USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64) WHERE is_active;
CREATE INDEX chunks_titan2_512_article_version ON chunks_titan2_512 (article_id, doc_version);
CREATE INDEX chunks_titan2_512_tsv ON chunks_titan2_512 USING gin (content_tsv) WHERE is_active;
CREATE INDEX chunks_titan2_512_tenant ON chunks_titan2_512 (tenant_id) WHERE is_active;

GRANT SELECT, INSERT, UPDATE, DELETE ON chunks_titan2_512 TO ingest_writer;
GRANT SELECT ON chunks_titan2_512 TO ingest_reader, ingest_writer;
