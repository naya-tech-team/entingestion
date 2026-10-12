-- 007: vector tables from embedding profiles (ingest.vectorstore.profile_tables).
--
-- After the numbered migrations, the migrate function creates chunks_<profile> for every
-- configured embedding profile that has no table yet, shaped exactly like chunks_primary
-- (after 001-006) with its own embedding dimension, and records it here. Tables that existed
-- before (chunks_primary, chunks_compact) are recorded the first time they're checked.

CREATE TABLE profile_tables (
  profile    text PRIMARY KEY,
  table_name text NOT NULL UNIQUE,
  dimension  int NOT NULL CHECK (dimension BETWEEN 1 AND 4000),
  model      text,
  created    boolean NOT NULL DEFAULT false, -- true: created from the profile, not a migration
  created_at timestamptz NOT NULL DEFAULT now()
);

-- ingest_ops reports the profile tables (tables function, GET /v1/tables).
GRANT SELECT ON profile_tables TO ingest_views;
