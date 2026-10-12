-- 005: tenants and filter columns (review M3.1).
--
-- Every chunk belongs to a tenant: the producer sets it from the source's access policy, and
-- articles without one are 'default'. Row-level security lets each reader see one tenant:
--
--   ingest_reader  rows of the tenant in app.tenant_id (SET LOCAL by the query function), or
--                  every row when the query function's internal actions (evaluation, drift
--                  check) set app.all_tenants = 'on'. Neither set: no rows (fail closed).
--   ingest_writer  every row (Index writes, updates and deletes for all tenants)
--   ingest_views   every row for reading (ingest_ops' status counts; it owns chunks_active)
--
-- The tables' owner (the master user, migrations) bypasses RLS. chunks_active is recreated
-- WITH (security_invoker = true), so a query through the view is checked as the caller, not as
-- the view's owner; without it the view would show readers every tenant.
--
-- published_at and source are typed copies of the chunk metadata, for search filters.

UPDATE chunks_titan2_1024 SET tenant_id = 'default' WHERE tenant_id IS NULL OR tenant_id = '';
UPDATE chunks_titan2_512 SET tenant_id = 'default' WHERE tenant_id IS NULL OR tenant_id = '';

ALTER TABLE chunks_titan2_1024
  ALTER COLUMN tenant_id SET NOT NULL,
  ADD CONSTRAINT chunks_titan2_1024_tenant_set CHECK (tenant_id <> ''),
  ADD COLUMN published_at date,
  ADD COLUMN source text;
ALTER TABLE chunks_titan2_512
  ALTER COLUMN tenant_id SET NOT NULL,
  ADD CONSTRAINT chunks_titan2_512_tenant_set CHECK (tenant_id <> ''),
  ADD COLUMN published_at date,
  ADD COLUMN source text;

-- Backfill from the metadata Index already wrote (published_at is ISO-8601 there).
UPDATE chunks_titan2_1024 SET
  source = metadata->>'source',
  published_at = CASE WHEN metadata->>'published_at' ~ '^\d{4}-\d{2}-\d{2}'
                      THEN left(metadata->>'published_at', 10)::date END;
UPDATE chunks_titan2_512 SET
  source = metadata->>'source',
  published_at = CASE WHEN metadata->>'published_at' ~ '^\d{4}-\d{2}-\d{2}'
                      THEN left(metadata->>'published_at', 10)::date END;

CREATE INDEX chunks_titan2_1024_published_at ON chunks_titan2_1024 (published_at) WHERE is_active;
CREATE INDEX chunks_titan2_1024_source ON chunks_titan2_1024 (source) WHERE is_active;
CREATE INDEX chunks_titan2_512_published_at ON chunks_titan2_512 (published_at) WHERE is_active;
CREATE INDEX chunks_titan2_512_source ON chunks_titan2_512 (source) WHERE is_active;

ALTER TABLE chunks_titan2_1024 ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks_titan2_512 ENABLE ROW LEVEL SECURITY;

CREATE POLICY reader_one_tenant ON chunks_titan2_1024 FOR SELECT TO ingest_reader
  USING (tenant_id = current_setting('app.tenant_id', true)
         OR current_setting('app.all_tenants', true) = 'on');
CREATE POLICY reader_one_tenant ON chunks_titan2_512 FOR SELECT TO ingest_reader
  USING (tenant_id = current_setting('app.tenant_id', true)
         OR current_setting('app.all_tenants', true) = 'on');
CREATE POLICY writer_all ON chunks_titan2_1024 TO ingest_writer USING (true) WITH CHECK (true);
CREATE POLICY writer_all ON chunks_titan2_512 TO ingest_writer USING (true) WITH CHECK (true);
CREATE POLICY views_read_all ON chunks_titan2_1024 FOR SELECT TO ingest_views USING (true);
CREATE POLICY views_read_all ON chunks_titan2_512 FOR SELECT TO ingest_views USING (true);

-- chunks_active again, on whichever table it serves now, checked as the caller. Its column
-- list is fixed when it's created, so this also adds the new columns to it.
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
  EXECUTE format(
    'CREATE VIEW chunks_active WITH (security_invoker = true) AS SELECT * FROM %I WHERE is_active',
    coalesce(live, 'chunks_titan2_1024'));
END $$;

ALTER VIEW chunks_active OWNER TO ingest_views;
GRANT SELECT ON chunks_active TO ingest_reader, ingest_writer;
