-- 001: the schema every vector table relies on. Applied by ingest.vectorstore.migrations (the
-- migrate function); never edit a migration after it has run, add the next one instead.
--
-- No vector table is created here. Each embedding profile has its own table, chunks_<profile>,
-- generated with the profile's dimension by ingest.vectorstore.profile_tables after the
-- migrations, and the migrate function then points the chunks_active view at the live one.

CREATE EXTENSION IF NOT EXISTS vector;

-- halfvec needs pgvector >= 0.7; iterative index scans (used at query time) need >= 0.8.
DO $$
DECLARE v int[] := string_to_array((SELECT extversion FROM pg_extension WHERE extname = 'vector'), '.')::int[];
BEGIN
  IF v < ARRAY[0, 8, 0] THEN
    RAISE EXCEPTION 'pgvector >= 0.8.0 required, found %', array_to_string(v, '.');
  END IF;
END $$;

-- Database users. Functions connect through RDS Proxy (or straight to the instance) with IAM
-- auth (rds_iam on RDS/Aurora); only migrate uses the master user.
--
--   ingest_writer  Index: writes, updates and deletes rows of every tenant
--   ingest_reader  the query function: one tenant at a time under row-level security
--   ingest_ops     the tables function: table status and the chunks_active switch; reads the
--                  vector tables, never writes them
--   ingest_views   NOLOGIN; owns chunks_active. Switching drops and recreates the view (the
--                  vector column's type differs per table), which only its owner may do, so
--                  the master user and ingest_ops are both members.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ingest_writer') THEN
    CREATE ROLE ingest_writer LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ingest_reader') THEN
    CREATE ROLE ingest_reader LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ingest_views') THEN
    CREATE ROLE ingest_views NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ingest_ops') THEN
    CREATE ROLE ingest_ops LOGIN;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam') THEN
    GRANT rds_iam TO ingest_writer, ingest_reader, ingest_ops;
  END IF;
END $$;

GRANT ingest_views TO ingest_ops;
GRANT ingest_views TO CURRENT_USER; -- the master user: hand the view over, and still switch it

-- The view's owner needs CREATE on its schema (to be given the view, and to create it again)
-- and SELECT on every table it can serve (each profile table grants it).
GRANT USAGE, CREATE ON SCHEMA public TO ingest_views;

-- The heading path, for the vector tables' weighted content_tsv (headings A, text B).
-- array_to_string is only STABLE, which a generated column can't use; for text[] it is
-- immutable, hence the wrapper.
CREATE FUNCTION ingest_heading_text(path text[]) RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE
  RETURN coalesce(array_to_string(path, ' '), '');

-- Deleted articles stay deleted: a late or redelivered run of a version no newer than the
-- delete can't write the article's chunks back into any vector table.
CREATE TABLE article_tombstones (
  article_id  text PRIMARY KEY,
  doc_version integer NOT NULL,
  deleted_at  timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON article_tombstones TO ingest_writer;

-- One row per embedding profile's vector table, written by ingest.vectorstore.profile_tables
-- when it creates (or first finds) the table.
CREATE TABLE profile_tables (
  profile    text PRIMARY KEY,
  table_name text NOT NULL UNIQUE,
  dimension  int NOT NULL CHECK (dimension BETWEEN 1 AND 4000),
  model      text,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- ingest_ops reports the profile tables (tables function, GET /v1/tables).
GRANT SELECT ON profile_tables TO ingest_views;
