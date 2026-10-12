-- 004: ingest_ops runs the vector-table operations the control API needs (status, switch
-- chunks_active) with IAM auth, so the master user's secret is only used to migrate.
--
-- Switching drops and recreates chunks_active (the vector column's type differs per table), and
-- only the view's owner may do that. The view therefore belongs to ingest_views, a NOLOGIN role
-- that the master user (who runs migrations) and ingest_ops are both members of. A migration
-- that adds a vector table must GRANT SELECT on it to ingest_views, or the view can't serve it.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ingest_views') THEN
    CREATE ROLE ingest_views NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ingest_ops') THEN
    CREATE ROLE ingest_ops LOGIN;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam') THEN
    GRANT rds_iam TO ingest_ops;
  END IF;
END $$;

GRANT ingest_views TO ingest_ops;
GRANT ingest_views TO CURRENT_USER; -- the master user: hand the view over, and still switch it

-- An owner needs CREATE on the view's schema (to be given the view, and to create it again)
-- and SELECT on every table it can serve. ingest_ops reads the tables through its membership.
GRANT USAGE, CREATE ON SCHEMA public TO ingest_views;
GRANT SELECT ON chunks_primary, chunks_compact TO ingest_views;

ALTER VIEW chunks_active OWNER TO ingest_views;
