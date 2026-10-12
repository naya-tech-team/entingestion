"""PgVectorStore.write_and_swap against a real pgvector database."""

from __future__ import annotations

from typing import Any

import pytest

from ingest.errors import IngestStale
from ingest.ids import chunk_id
from ingest.models.staging import Chunk
from ingest.vectorstore.index import ArticleAttributes, IndexRow, PgVectorStore

pytestmark = pytest.mark.integration

TABLE = "chunks_primary"


def rows(
    article_id: str,
    version: int,
    count: int,
    *,
    heading: list[str] | None = None,
    tenant: str = "t1",
) -> list[IndexRow]:
    out = []
    for ordinal in range(count):
        chunk = Chunk(
            chunk_id=chunk_id(article_id, version, ordinal),
            article_id=article_id,
            doc_version=version,
            ordinal=ordinal,
            text=f"chunk {ordinal} of v{version}",
            embed_text=f"Doc > chunk {ordinal}",
            heading_path=heading or [],
            token_count=4,
            metadata={"published_at": "2026-01-01", "n": ordinal},
        )
        out.append(
            IndexRow(
                chunk,
                [0.01 * (ordinal + 1)] * 1024,
                "prc_1",
                ArticleAttributes(tenant, "news", "2026-01-01T00:00:00Z", {"lang": "en"}),
                "amazon.titan-embed-text-v2:0",
                "v1",
            )
        )
    return out


def active(pg: Any, article_id: str) -> list[tuple[int, int]]:
    return pg.execute(
        f"SELECT doc_version, ordinal FROM {TABLE} WHERE article_id = %s AND is_active"  # noqa: S608
        " ORDER BY ordinal",
        (article_id,),
    ).fetchall()


def test_new_version_replaces_the_old_one(pg: Any) -> None:
    store = PgVectorStore(pg, TABLE)
    store.write_and_swap("art_a", 1, rows("art_a", 1, 3))

    result = store.write_and_swap("art_a", 2, rows("art_a", 2, 2))

    assert result.written == 2
    assert result.deleted_old == 3
    assert active(pg, "art_a") == [(2, 0), (2, 1)]


def test_rerun_of_a_version_with_fewer_chunks_leaves_no_leftovers(pg: Any) -> None:
    store = PgVectorStore(pg, TABLE)
    store.write_and_swap("art_b", 1, rows("art_b", 1, 4))

    store.write_and_swap("art_b", 1, rows("art_b", 1, 2))

    assert active(pg, "art_b") == [(1, 0), (1, 1)]
    total = pg.execute(f"SELECT count(*) FROM {TABLE} WHERE article_id = 'art_b'").fetchone()  # noqa: S608
    assert total[0] == 2


def test_older_version_is_refused(pg: Any) -> None:
    store = PgVectorStore(pg, TABLE)
    store.write_and_swap("art_c", 2, rows("art_c", 2, 1))

    with pytest.raises(IngestStale):
        store.write_and_swap("art_c", 1, rows("art_c", 1, 1))
    assert active(pg, "art_c") == [(2, 0)]


def test_columns_round_trip_and_no_temp_tables(pg: Any) -> None:
    store = PgVectorStore(pg, TABLE)
    store.write_and_swap("art_d", 1, rows("art_d", 1, 1, heading=["Guide", "Install"]))

    row = pg.execute(
        f"SELECT heading_path, metadata->>'n', tenant_id, vector_dims(embedding::vector)"  # noqa: S608
        f" FROM {TABLE} WHERE article_id = 'art_d'"
    ).fetchone()
    assert row == (["Guide", "Install"], "0", "t1", 1024)
    temp = pg.execute("SELECT count(*) FROM pg_class WHERE relpersistence = 't'").fetchone()
    assert temp[0] == 0


def test_large_articles_are_written_in_batches(pg: Any) -> None:
    store = PgVectorStore(pg, TABLE)
    result = store.write_and_swap("art_e", 1, rows("art_e", 1, 1201))

    assert result.written == 1201
    assert len(active(pg, "art_e")) == 1201


def test_wrong_dimension_is_rejected(pg: Any) -> None:
    store = PgVectorStore(pg, TABLE)
    bad = rows("art_f", 1, 1)
    bad[0] = IndexRow(bad[0].chunk, [0.1] * 512, None, bad[0].attributes, "m", "v1")

    with pytest.raises(Exception, match="dimensions"):
        store.write_and_swap("art_f", 1, bad)
    pg.rollback()
    assert active(pg, "art_f") == []


def test_a_deleted_version_cannot_be_written_back(pg: Any) -> None:
    store = PgVectorStore(pg, TABLE)
    store.write_and_swap("art_h", 2, rows("art_h", 2, 2))

    assert store.delete_article("art_h", 2) == 2
    with pytest.raises(IngestStale):
        store.write_and_swap("art_h", 2, rows("art_h", 2, 2))  # a late run of the deleted version
    assert active(pg, "art_h") == []


def test_a_newer_version_after_a_delete_retires_the_tombstone(pg: Any) -> None:
    store = PgVectorStore(pg, TABLE)
    store.write_and_swap("art_i", 1, rows("art_i", 1, 1))
    store.delete_article("art_i", 1)

    store.write_and_swap("art_i", 2, rows("art_i", 2, 1))

    assert active(pg, "art_i") == [(2, 0)]
    left = pg.execute("SELECT count(*) FROM article_tombstones WHERE article_id = 'art_i'")
    assert left.fetchone()[0] == 0


def test_tombstones_only_move_forward(pg: Any) -> None:
    store = PgVectorStore(pg, TABLE)
    store.delete_article("art_j", 3)
    store.delete_article("art_j", 1)  # a stale delete

    tomb = pg.execute("SELECT doc_version FROM article_tombstones WHERE article_id = 'art_j'")
    assert tomb.fetchone()[0] == 3


def test_versions_pages_active_rows_by_article(pg: Any) -> None:
    from ingest.query import versions

    store = PgVectorStore(pg, TABLE)
    store.write_and_swap("art_v1", 1, rows("art_v1", 1, 2))
    store.write_and_swap("art_v2", 3, rows("art_v2", 3, 1))
    store.write_and_swap("art_v3", 1, rows("art_v3", 1, 4))

    first = versions(pg, TABLE, "", 2)
    second = versions(pg, TABLE, first["next"], 2)

    assert first == {"rows": [["art_v1", 1, 2], ["art_v2", 3, 1]], "next": "art_v2"}
    assert second == {"rows": [["art_v3", 1, 4]], "next": None}
