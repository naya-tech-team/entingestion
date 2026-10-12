"""Vector tables from embedding profiles: the migrate function, after the numbered migrations,
makes sure every configured profile has its table (ingest.embedding.profiles).

A new profile's table, chunks_<profile>, gets exactly the structure chunks_titan2_1024 has after
migrations 001-006, with the profile's dimension:

    columns, defaults, NOT NULLs and the weighted content_tsv  LIKE chunks_titan2_1024
    embedding halfvec(<dimension>)
    primary key (chunk_id), CHECK (tenant_id <> '')
    partial HNSW (halfvec_cosine_ops, m 16, ef_construction 64) and GIN tsv WHERE is_active,
    (article_id, doc_version), and tenant / source / published_at WHERE is_active
    row-level security: reader_one_tenant, writer_all, views_read_all (migration 005)
    grants: ingest_writer read/write; ingest_reader, ingest_writer and ingest_views read

Idempotent: an existing table is checked, not changed. A table whose embedding dimension
differs from its profile's is refused (ProfileTableError): a profile's dimension can't change;
make a new profile instead. Every table is recorded in profile_tables (migration 007).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from psycopg import sql

from ingest.embedding.profiles import EmbeddingProfile
from ingest.vectorstore.migrations import LOCK_KEY

if TYPE_CHECKING:
    import psycopg

TEMPLATE = "chunks_titan2_1024"
HNSW = "WITH (m = 16, ef_construction = 64)"
_READER = (
    "tenant_id = current_setting('app.tenant_id', true)"
    " OR current_setting('app.all_tenants', true) = 'on'"
)


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


def _statements(table: str, dimension: int) -> list[sql.Composed]:
    t = sql.Identifier(table)

    def name(suffix: str) -> sql.Identifier:
        return sql.Identifier(f"{table}_{suffix}")

    def index(suffix: str, body: str) -> sql.Composed:
        return sql.SQL("CREATE INDEX {i} ON {t} " + body).format(i=name(suffix), t=t)

    return [
        sql.SQL("CREATE TABLE {t} (LIKE {template} INCLUDING DEFAULTS INCLUDING GENERATED)").format(
            t=t, template=sql.Identifier(TEMPLATE)
        ),
        sql.SQL("ALTER TABLE {t} ALTER COLUMN embedding TYPE halfvec({d})").format(
            t=t, d=sql.Literal(dimension)
        ),
        sql.SQL("ALTER TABLE {t} ADD PRIMARY KEY (chunk_id)").format(t=t),
        sql.SQL("ALTER TABLE {t} ADD CONSTRAINT {c} CHECK (tenant_id <> '')").format(
            t=t, c=name("tenant_set")
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
        # ingest_views: the chunks_active view (owned by it) may serve the table (migration 004).
        sql.SQL("GRANT SELECT ON {t} TO ingest_reader, ingest_writer, ingest_views").format(t=t),
    ]


def _record(conn: psycopg.Connection[Any], profile: EmbeddingProfile, *, created: bool) -> None:
    conn.execute(
        "INSERT INTO profile_tables (profile, table_name, dimension, model, created)"
        " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (profile) DO NOTHING",
        (profile.name, profile.table, profile.dimension, profile.model, created),
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
                    for statement in _statements(profile.table, profile.dimension):
                        conn.execute(statement)
                    created.append(profile.table)
                _record(conn, profile, created=profile.table in created)
        return created
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
