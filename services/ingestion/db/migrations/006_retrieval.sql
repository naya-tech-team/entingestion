-- 006: retrieval (review M3.2, M3.3).
--
-- content_tsv weights the heading path (A) above the text (B), so a keyword that names a
-- section ranks that section's chunks first (ts_rank_cd in the query function's hybrid search).
-- array_to_string is only STABLE, which a generated column can't use; for text[] it is
-- immutable, hence the wrapper.
--
-- parent_text: parent-child chunking (chunker.py) stores each child's section-level parent here;
-- search returns it as the child's context. char_start/char_end: the chunk's span in the
-- normalized text, for citations.
--
-- Replacing content_tsv rewrites both tables. chunks_active depends on the column, so it is
-- dropped and recreated (security_invoker, migration 005) on the table it serves.

CREATE FUNCTION ingest_heading_text(path text[]) RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE
  RETURN coalesce(array_to_string(path, ' '), '');

DO $$
DECLARE live text;
BEGIN
  SELECT c.relname INTO live
  FROM pg_depend d
  JOIN pg_rewrite r ON r.oid = d.objid
  JOIN pg_class c ON c.oid = d.refobjid
  WHERE r.ev_class = 'chunks_active'::regclass AND c.relkind = 'r'
  LIMIT 1;
  DROP VIEW chunks_active;

  DROP INDEX chunks_primary_tsv;
  DROP INDEX chunks_compact_tsv;
  ALTER TABLE chunks_primary
    DROP COLUMN content_tsv,
    ADD COLUMN content_tsv tsvector GENERATED ALWAYS AS (
      setweight(to_tsvector('english'::regconfig, public.ingest_heading_text(heading_path)), 'A')
      || setweight(to_tsvector('english'::regconfig, text), 'B')) STORED,
    ADD COLUMN parent_text text,
    ADD COLUMN char_start int,
    ADD COLUMN char_end int;
  ALTER TABLE chunks_compact
    DROP COLUMN content_tsv,
    ADD COLUMN content_tsv tsvector GENERATED ALWAYS AS (
      setweight(to_tsvector('english'::regconfig, public.ingest_heading_text(heading_path)), 'A')
      || setweight(to_tsvector('english'::regconfig, text), 'B')) STORED,
    ADD COLUMN parent_text text,
    ADD COLUMN char_start int,
    ADD COLUMN char_end int;
  CREATE INDEX chunks_primary_tsv ON chunks_primary USING gin (content_tsv) WHERE is_active;
  CREATE INDEX chunks_compact_tsv ON chunks_compact USING gin (content_tsv) WHERE is_active;

  EXECUTE format(
    'CREATE VIEW chunks_active WITH (security_invoker = true) AS SELECT * FROM %I WHERE is_active',
    coalesce(live, 'chunks_primary'));
END $$;

ALTER VIEW chunks_active OWNER TO ingest_views;
GRANT SELECT ON chunks_active TO ingest_reader, ingest_writer;
