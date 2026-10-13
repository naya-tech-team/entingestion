"""Vector tables from embedding profiles. The migrate function, after the migrations, makes sure
every configured profile (ingest.embedding.profiles) has its table, chunks_<profile>, the
default profile's included. Every table comes from the one generator here, with the profile's
dimension:

    columns          chunk, article and tenant identity, text and citation offsets, metadata,
                     embedding halfvec(<dimension>), and content_tsv generated from the heading
                     path (weight A) and the text (weight B)
    constraints      primary key (chunk_id), tenant_id NOT NULL and CHECK (tenant_id <> '')
    indexes          partial HNSW (halfvec_cosine_ops, m 16, ef_construction 64) and GIN
                     content_tsv WHERE is_active; (article_id, doc_version); tenant, source and
                     published_at WHERE is_active
    row-level        reader_one_tenant: ingest_reader sees the tenant in app.tenant_id, or every
    security         tenant when app.all_tenants = 'on' (the query function's internal actions);
                     neither set, no rows. writer_all: ingest_writer reads and writes every row.
                     views_read_all: ingest_views reads every row (ingest_ops' status counts; it
                     owns chunks_active). The tables' owner (the master user) bypasses RLS.
    grants           ingest_writer read/write; ingest_reader, ingest_writer and ingest_views read

Idempotent: an existing table is checked, not changed. A table whose embedding dimension
differs from its profile's is refused (ProfileTableError): a profile's dimension can't change;
make a new profile instead. Every table is recorded in profile_tables.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from psycopg import sql

from ingest.embedding.profiles import EmbeddingProfile
from ingest.vectorstore.migrations import LOCK_KEY

if TYPE_CHECKING:
    import psycopg

HNSW = "WITH (m = 16, ef_construction = 64)"
_READER = (
    "tenant_id = current_setting('app.tenant_id', true)"
    " OR current_setting('app.all_tenants', true) = 'on'"
)
# Every vector table's columns, in order; {dimension} is the profile's.
_COLUMNS = """
  chunk_id         text NOT NULL,
  article_id       text NOT NULL,
  doc_version      int NOT NULL,
  ordinal          int NOT NULL,
  parent_id        text,
  process_id       text,
  tenant_id        text NOT NULL,
  is_active        boolean NOT NULL DEFAULT false,
  text             text NOT NULL,
  embed_text       text NOT NULL,
  heading_path     text[],
  page_start       int,
  page_end         int,
  token_count      int NOT NULL,
  metadata         jsonb NOT NULL DEFAULT '{{}}',
  embedding        halfvec({dimension}) NOT NULL,
  embedding_model  text NOT NULL,
  pipeline_version text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  published_at     date,
  source           text,
  content_tsv      tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('english'::regconfig, public.ingest_heading_text(heading_path)), 'A')
    || setweight(to_tsvector('english'::regconfig, text), 'B')) STORED,
  parent_text      text,
  char_start       int,
  char_end         int,
  CONSTRAINT {pkey} PRIMARY KEY (chunk_id),
  CONSTRAINT {tenant_set} CHECK (tenant_id <> '')
"""


class ProfileTableError(RuntimeError):
    pass


def dimension_of(conn: psycopg.Connection[Any], table: str) -> int | None:
    """The embedding column's dimension, or None if the table doesn't exist."""
    row = conn.execute(
        "SELECT a.atttypmod FROM pg_attribute a"
        " WHERE a.attrelid = to_regclass(%s) AND a.attname = 'embedding' AND NOT a.attisdropped",
        (table,),
    ).fetchone()
    return int(row[0]) if row else None


def ddl(table: str, dimension: int) -> list[sql.Composed]:
    """The statements that create one vector table with its indexes, policies and grants."""
    t = sql.Identifier(table)

    def name(suffix: str) -> sql.Identifier:
        return sql.Identifier(f"{table}_{suffix}")

    def index(suffix: str, body: str) -> sql.Composed:
        return sql.SQL("CREATE INDEX {i} ON {t} " + body).format(i=name(suffix), t=t)

    return [
        sql.SQL("CREATE TABLE {t} (" + _COLUMNS + ")").format(
            t=t,
            dimension=sql.Literal(dimension),
            pkey=name("pkey"),
            tenant_set=name("tenant_set"),
        ),
        index(
            "embedding_hnsw",
            f"USING hnsw (embedding halfvec_cosine_ops) {HNSW} WHERE is_active",
        ),
        index("article_version", "(article_id, doc_version)"),
        index("tsv", "USING gin (content_tsv) WHERE is_active"),
        index("tenant", "(tenant_id) WHERE is_active"),
        index("published_at", "(published_at) WHERE is_active"),
        index("source", "(source) WHERE is_active"),
        sql.SQL("ALTER TABLE {t} ENABLE ROW LEVEL SECURITY").format(t=t),
        sql.SQL(
            "CREATE POLICY reader_one_tenant ON {t} FOR SELECT TO ingest_reader USING ("
            + _READER
            + ")"
        ).format(t=t),
        sql.SQL(
            "CREATE POLICY writer_all ON {t} TO ingest_writer USING (true) WITH CHECK (true)"
        ).format(t=t),
        sql.SQL(
            "CREATE POLICY views_read_all ON {t} FOR SELECT TO ingest_views USING (true)"
        ).format(t=t),
        sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO ingest_writer").format(t=t),
        # ingest_views: chunks_active (owned by it) may serve the table.
        sql.SQL("GRANT SELECT ON {t} TO ingest_reader, ingest_writer, ingest_views").format(t=t),
    ]


def _record(conn: psycopg.Connection[Any], profile: EmbeddingProfile) -> None:
    conn.execute(
        "INSERT INTO profile_tables (profile, table_name, dimension, model)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT (profile) DO NOTHING",
        (profile.name, profile.table, profile.dimension, profile.model),
    )


def ensure(conn: psycopg.Connection[Any], profiles: Iterable[EmbeddingProfile]) -> list[str]:
    """Create the missing profile tables. Returns the tables created. The connection is in
    autocommit (migrations.apply); each table is created in its own transaction."""
    conn.autocommit = True
    conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        wanted = sorted(profiles, key=lambda p: p.name)
        for profile in wanted:  # check everything before creating anything
            existing = dimension_of(conn, profile.table)
            if existing is not None and existing != profile.dimension:
                raise ProfileTableError(
                    f"{profile.table} has {existing}-d embeddings but profile {profile.name} is"
                    f" {profile.dimension}-d; a profile's dimension can't change, add a new"
                    " profile (a new table) instead"
                )
        created: list[str] = []
        for profile in wanted:
            with conn.transaction():
                if dimension_of(conn, profile.table) is None:
                    for statement in ddl(profile.table, profile.dimension):
                        conn.execute(statement)
                    created.append(profile.table)
                _record(conn, profile)
        return created
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
