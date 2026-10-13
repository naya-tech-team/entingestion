"""Index: transactional write + swap, and the delete path.

    BEGIN; SELECT pg_advisory_xact_lock(hashtext(:article_id));
      batched INSERT ... ON CONFLICT (chunk_id) DO UPDATE  (version N+1, inactive)
      verify count = :chunk_count
      UPDATE ... SET is_active = (doc_version = :v) WHERE article_id = :a
      DELETE ... WHERE article_id = :a AND doc_version < :v
    COMMIT;

Readers keep seeing version N until the commit, then only N+1: never a mix, never nothing.

A metadata-only change (Claim's METADATA action) rewrites the article's descriptive columns on
its active rows in place: update_metadata.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from psycopg import sql

from ingest.config import get_settings
from ingest.errors import IngestStale, make_error, validation_code
from ingest.models.staging import Chunk
from ingest.timeutil import iso_day
from ingest.vectorstore.db import connection
from ingest.vectorstore.targets import TARGETS

COLUMNS = (
    "chunk_id",
    "article_id",
    "doc_version",
    "ordinal",
    "parent_id",
    "process_id",
    "tenant_id",
    "source",
    "published_at",
    "text",
    "embed_text",
    "heading_path",
    "page_start",
    "page_end",
    "char_start",
    "char_end",
    "parent_text",
    "token_count",
    "metadata",
    "embedding",
    "embedding_model",
    "pipeline_version",
)


INSERT_BATCH = 500  # 500 rows x 22 parameters stays far below PostgreSQL's 65,535 limit
_CASTS = {
    "metadata": "::jsonb",
    "embedding": "::halfvec",
    "heading_path": "::text[]",
    "published_at": "::date",
}
DEFAULT_TENANT = "default"  # articles whose producer row has no tenant (tenant_id NOT NULL)
# Chunk metadata keys that come from the article row, not the content.
ARTICLE_KEYS = ("tenant_id", "source", "published_at", "article")


@dataclass(frozen=True)
class ArticleAttributes:
    """An article's descriptive fields as Index writes them: the tenant (never empty), the
    filter columns, and the producer metadata (chunk metadata "article")."""

    tenant_id: str
    source: str | None
    published_at: str | None  # ISO-8601 UTC
    metadata: dict[str, Any]

    @property
    def published_day(self) -> str | None:
        return iso_day(self.published_at)

    def chunk_metadata(self) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "tenant_id": self.tenant_id,
            "source": self.source,
            "published_at": self.published_at,
            "article": self.metadata or None,
        }
        return {k: v for k, v in meta.items() if v is not None}

    def apply(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """A chunk's metadata with the article's fields replaced by these."""
        kept = {k: v for k, v in metadata.items() if k not in ARTICLE_KEYS}
        return kept | self.chunk_metadata()


_ROW_PLACEHOLDERS = sql.SQL("(" + ", ".join("%s" + _CASTS.get(c, "") for c in COLUMNS) + ")")


@dataclass(frozen=True)
class IndexRow:
    chunk: Chunk
    embedding: list[float]
    process_id: str | None
    attributes: ArticleAttributes
    embedding_model: str
    pipeline_version: str

    def values(self) -> tuple[Any, ...]:
        c, a = self.chunk, self.attributes
        return (
            c.chunk_id,
            c.article_id,
            c.doc_version,
            c.ordinal,
            c.parent_id,
            self.process_id,
            a.tenant_id or DEFAULT_TENANT,
            a.source,
            a.published_day,
            c.text,
            c.embed_text,
            c.heading_path or None,
            c.page_start,
            c.page_end,
            c.char_start,
            c.char_end,
            c.parent_text,
            c.token_count,
            json.dumps(a.apply(c.metadata)),
            "[" + ",".join(f"{x:.7g}" for x in self.embedding) + "]",
            self.embedding_model,
            self.pipeline_version,
        )


@dataclass(frozen=True)
class IndexResult:
    written: int
    deleted_old: int


class VectorStore(Protocol):
    def write_and_swap(
        self, article_id: str, doc_version: int, rows: list[IndexRow]
    ) -> IndexResult: ...

    def delete_article(self, article_id: str, doc_version: int = 0) -> int: ...

    def update_metadata(self, article_id: str, attributes: ArticleAttributes) -> int: ...

    def retag_tenant(self, article_id: str, tenant_id: str) -> int: ...


class PgVectorStore:
    def __init__(self, conn: Any, table: str) -> None:
        self.conn = conn
        self.table = sql.Identifier(table)

    def write_and_swap(
        self, article_id: str, doc_version: int, rows: list[IndexRow]
    ) -> IndexResult:
        cols = sql.SQL(", ").join(sql.Identifier(c) for c in COLUMNS)
        updates = sql.SQL(", ").join(
            sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c))
            for c in COLUMNS
            if c != "chunk_id"
        )
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (article_id,))
            newer = self.conn.execute(
                sql.SQL(
                    "SELECT 1 FROM {t} WHERE article_id = %s AND doc_version > %s LIMIT 1"
                ).format(t=self.table),
                (article_id, doc_version),
            ).fetchone()
            if newer:
                # A newer version is already indexed; never roll an article back.
                raise IngestStale(f"{article_id}: v{doc_version} is older than the indexed version")
            tomb = self.conn.execute(
                "SELECT doc_version FROM article_tombstones WHERE article_id = %s", (article_id,)
            ).fetchone()
            if tomb and tomb[0] >= doc_version:
                raise IngestStale(
                    f"{article_id}: v{doc_version} was deleted (tombstone v{tomb[0]})"
                )
            # Plain multi-row INSERTs: no temp table or COPY, which would pin the RDS Proxy
            # connection to this Lambda for its whole life.
            for start in range(0, len(rows), INSERT_BATCH):
                batch = rows[start : start + INSERT_BATCH]
                values = sql.SQL(", ").join(_ROW_PLACEHOLDERS for _ in batch)
                self.conn.execute(
                    sql.SQL(
                        "INSERT INTO {t} ({cols}) VALUES {values}"
                        " ON CONFLICT (chunk_id) DO UPDATE SET {updates}, is_active = false"
                    ).format(t=self.table, cols=cols, values=values, updates=updates),
                    [v for row in batch for v in row.values()],
                )
            # A re-run of the same version may have produced fewer chunks: drop the leftovers.
            self.conn.execute(
                sql.SQL(
                    "DELETE FROM {t} WHERE article_id = %s AND doc_version = %s"
                    " AND NOT (chunk_id = ANY(%s::text[]))"
                ).format(t=self.table),
                (article_id, doc_version, [r.chunk.chunk_id for r in rows]),
            )
            count = self.conn.execute(
                sql.SQL(
                    "SELECT count(*) FROM {t} WHERE article_id = %s AND doc_version = %s"
                ).format(t=self.table),
                (article_id, doc_version),
            ).fetchone()[0]
            if count != len(rows):
                raise make_error(
                    validation_code("index_count"),
                    f"{count} rows for v{doc_version}, expected {len(rows)}",
                )
            self.conn.execute(
                sql.SQL(
                    "UPDATE {t} SET is_active = (doc_version = %s)"
                    " WHERE article_id = %s AND is_active IS DISTINCT FROM (doc_version = %s)"
                ).format(t=self.table),
                (doc_version, article_id, doc_version),
            )
            deleted = self.conn.execute(
                sql.SQL("DELETE FROM {t} WHERE article_id = %s AND doc_version < %s").format(
                    t=self.table
                ),
                (article_id, doc_version),
            ).rowcount
            # A newer version after a delete (the article was re-uploaded) retires the tombstone.
            self.conn.execute(
                "DELETE FROM article_tombstones WHERE article_id = %s AND doc_version < %s",
                (article_id, doc_version),
            )
        return IndexResult(written=len(rows), deleted_old=deleted)

    def update_metadata(self, article_id: str, attributes: ArticleAttributes) -> int:
        """Rewrite the article's descriptive columns (tenant, filters, metadata) on its active
        rows, keeping the content and vectors. Returns the rows updated."""
        removed = sql.SQL(" - ").join(sql.Literal(k) for k in ARTICLE_KEYS)
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (article_id,))
            return int(
                self.conn.execute(
                    sql.SQL(
                        "UPDATE {t} SET tenant_id = %s, source = %s, published_at = %s::date,"
                        " metadata = (metadata - {removed}) || %s::jsonb"
                        " WHERE article_id = %s AND is_active"
                    ).format(t=self.table, removed=removed),
                    (
                        attributes.tenant_id or DEFAULT_TENANT,
                        attributes.source,
                        attributes.published_day,
                        json.dumps(attributes.chunk_metadata()),
                        article_id,
                    ),
                ).rowcount
            )

    def retag_tenant(self, article_id: str, tenant_id: str) -> int:
        """Give the article's rows another tenant, leaving everything else. For tables the
        article's tenant doesn't write (any more): after the article moved to a tenant whose
        registry writes other tables, its old rows here must not stay visible to the old tenant.
        Rows are kept (a rollback to this table still has them). Returns the rows changed."""
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (article_id,))
            return int(
                self.conn.execute(
                    sql.SQL(
                        "UPDATE {t} SET tenant_id = %s,"
                        " metadata = jsonb_set(metadata, '{{tenant_id}}', to_jsonb(%s::text))"
                        " WHERE article_id = %s AND tenant_id <> %s"
                    ).format(t=self.table),
                    (tenant_id, tenant_id, article_id, tenant_id),
                ).rowcount
            )

    def delete_article(self, article_id: str, doc_version: int = 0) -> int:
        """Remove the article's rows and leave a tombstone at doc_version, in one transaction."""
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (article_id,))
            self.conn.execute(
                "INSERT INTO article_tombstones (article_id, doc_version) VALUES (%s, %s)"
                " ON CONFLICT (article_id) DO UPDATE SET deleted_at = now(),"
                " doc_version = GREATEST(article_tombstones.doc_version, EXCLUDED.doc_version)",
                (article_id, doc_version),
            )
            return int(
                self.conn.execute(
                    sql.SQL("DELETE FROM {t} WHERE article_id = %s").format(t=self.table),
                    (article_id,),
                ).rowcount
            )


def get_vector_store(table: str | None = None) -> VectorStore:
    settings = get_settings()
    settings.require("db.host")
    return PgVectorStore(connection(settings), table or settings.target_table)


def delete_everywhere(article_id: str, doc_version: int = 0) -> int:
    """A deleted article leaves every vector table, live or not (docs/USE_CASES.md UC8), and stays
    gone:
    each table's delete also records the tombstone at doc_version."""
    return sum(get_vector_store(table).delete_article(article_id, doc_version) for table in TARGETS)
