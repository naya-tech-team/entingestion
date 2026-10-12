-- 003: deleted articles stay deleted. A late or redelivered run of a version no newer than the
-- delete can't write the article's chunks back into any vector table.

CREATE TABLE article_tombstones (
  article_id  text PRIMARY KEY,
  doc_version integer NOT NULL,
  deleted_at  timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON article_tombstones TO ingest_writer;
