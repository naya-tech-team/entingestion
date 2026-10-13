"""Vector tables from embedding profiles (ingest.vectorstore.profile_tables) on a real pgvector
database prepared as the migrate function does it (the pg fixture: the test profiles' tables):
a custom 384-d profile's table has the same structure as the default profiles' tables, takes
writes and serves one tenant to ingest_reader, and ensure() is idempotent and refuses a changed
dimension."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

from ingest.config import get_settings
from ingest.embedding.profiles import EmbeddingProfile, parse
from ingest.query import search_hybrid
from ingest.vectorstore.index import PgVectorStore
from ingest.vectorstore.profile_tables import ProfileTableError, dimension_of, ensure
from tests.conftest import PROFILE_ENV, PROFILES
from tests.integration.test_index import rows
from tests.integration.test_tenants import as_role

pytestmark = pytest.mark.integration

MINI = EmbeddingProfile(
    "mini_384", "openai_compatible", "all-minilm", 384, base_url="https://emb.example/v1"
)
TABLE = "chunks_mini_384"
DEFAULTS = list(parse(PROFILE_ENV).profiles.values())
# Every vector table's columns: (name, type, nullable, default, generated).
COLUMNS = [
    ("chunk_id", "text", "NO", None, "NEVER"),
    ("article_id", "text", "NO", None, "NEVER"),
    ("doc_version", "integer", "NO", None, "NEVER"),
    ("ordinal", "integer", "NO", None, "NEVER"),
    ("parent_id", "text", "YES", None, "NEVER"),
    ("process_id", "text", "YES", None, "NEVER"),
    ("tenant_id", "text", "NO", None, "NEVER"),
    ("is_active", "boolean", "NO", "false", "NEVER"),
    ("text", "text", "NO", None, "NEVER"),
    ("embed_text", "text", "NO", None, "NEVER"),
    ("heading_path", "ARRAY", "YES", None, "NEVER"),
    ("page_start", "integer", "YES", None, "NEVER"),
    ("page_end", "integer", "YES", None, "NEVER"),
    ("token_count", "integer", "NO", None, "NEVER"),
    ("metadata", "jsonb", "NO", "'{}'::jsonb", "NEVER"),
    ("embedding", "USER-DEFINED", "NO", None, "NEVER"),
    ("embedding_model", "text", "NO", None, "NEVER"),
    ("pipeline_version", "text", "NO", None, "NEVER"),
    ("created_at", "timestamp with time zone", "NO", "now()", "NEVER"),
    ("published_at", "date", "YES", None, "NEVER"),
    ("source", "text", "YES", None, "NEVER"),
    ("content_tsv", "tsvector", "YES", None, "ALWAYS"),
    ("parent_text", "text", "YES", None, "NEVER"),
    ("char_start", "integer", "YES", None, "NEVER"),
    ("char_end", "integer", "YES", None, "NEVER"),
]


@pytest.fixture(autouse=True)
def mini_profile(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    profiles = {
        **PROFILES,
        "mini_384": {
            "provider": "openai_compatible",
            "model": "all-minilm",
            "dimension": 384,
            "base_url": "https://emb.example/v1",
        },
    }
    monkeypatch.setenv("EMBEDDING_PROFILES", json.dumps(profiles))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def columns(pg: Any, table: str) -> list[tuple[Any, ...]]:
    return pg.execute(
        "SELECT column_name, data_type, is_nullable, column_default, is_generated,"
        " generation_expression FROM information_schema.columns"
        " WHERE table_name = %s ORDER BY ordinal_position",
        (table,),
    ).fetchall()


def indexes(pg: Any, table: str) -> set[str]:
    """Index definitions with the table's name taken out, to compare two tables."""
    found = pg.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s", (table,))
    return {f"{n}: {d}".replace(table, "T") for n, d in found.fetchall()}


def policies(pg: Any, table: str) -> set[tuple[Any, ...]]:
    return set(
        pg.execute(
            "SELECT policyname, cmd, roles::text, qual, with_check FROM pg_policies"
            " WHERE tablename = %s",
            (table,),
        ).fetchall()
    )


def grants(pg: Any, table: str) -> set[tuple[str, str]]:
    return set(
        pg.execute(
            "SELECT grantee, privilege_type FROM information_schema.role_table_grants"
            " WHERE table_name = %s AND grantee LIKE 'ingest_%%'",
            (table,),
        ).fetchall()
    )


def test_every_profile_table_has_the_same_structure(pg: Any) -> None:
    assert ensure(pg, [MINI, *DEFAULTS]) == [TABLE]  # the defaults exist already

    assert (dimension_of(pg, TABLE), dimension_of(pg, "chunks_primary")) == (384, 1024)
    assert dimension_of(pg, "chunks_compact") == 512
    template = columns(pg, "chunks_primary")
    assert [c[:5] for c in template] == COLUMNS
    assert "ingest_heading_text(heading_path)" in str(template[21][5])
    for table in (TABLE, "chunks_compact"):
        assert columns(pg, table) == template  # halfvec's size isn't in information_schema
        assert indexes(pg, table) == indexes(pg, "chunks_primary")
        assert policies(pg, table) == policies(pg, "chunks_primary")
        assert grants(pg, table) == grants(pg, "chunks_primary")
    assert {i.split(":")[0] for i in indexes(pg, TABLE)} == {
        f"T_{s}"
        for s in ("pkey", "embedding_hnsw", "article_version", "tsv", "tenant", "published_at",
                  "source")
    }  # fmt: skip
    hnsw = next(i for i in indexes(pg, TABLE) if i.startswith("T_embedding_hnsw"))
    assert "halfvec_cosine_ops" in hnsw
    assert "m='16', ef_construction='64'" in hnsw
    assert hnsw.endswith("WHERE is_active")
    assert {p[0] for p in policies(pg, TABLE)} == {
        "reader_one_tenant",
        "writer_all",
        "views_read_all",
    }
    assert grants(pg, TABLE) == {
        ("ingest_writer", p) for p in ("SELECT", "INSERT", "UPDATE", "DELETE")
    } | {("ingest_reader", "SELECT"), ("ingest_views", "SELECT")}

    def constraints(table: str, kinds: str) -> list[tuple[str, str]]:
        return pg.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conrelid = %s::regclass AND contype = ANY(%s) ORDER BY conname",
            (table, list(kinds)),
        ).fetchall()

    # NOT NULLs (named constraints since PostgreSQL 18).
    not_null = sorted(d for _, d in constraints("chunks_primary", "n"))
    assert sorted(d for _, d in constraints(TABLE, "n")) == not_null
    assert constraints(TABLE, "pc") == [
        ("chunks_mini_384_pkey", "PRIMARY KEY (chunk_id)"),
        ("chunks_mini_384_tenant_set", "CHECK ((tenant_id <> ''::text))"),
    ]
    rls = pg.execute("SELECT relrowsecurity FROM pg_class WHERE relname = %s", (TABLE,))
    assert rls.fetchone() == (True,)
    recorded = pg.execute(
        "SELECT profile, table_name, dimension, model FROM profile_tables ORDER BY profile"
    ).fetchall()
    assert recorded == [
        ("compact", "chunks_compact", 512, PROFILES["compact"]["model"]),
        ("mini_384", TABLE, 384, "all-minilm"),
        ("primary", "chunks_primary", 1024, PROFILES["primary"]["model"]),
    ]


def mini_rows(article_id: str, count: int, tenant: str) -> list[Any]:
    return [
        replace(r, embedding=[0.05 * (i + 1)] * 384)
        for i, r in enumerate(rows(article_id, 1, count, tenant=tenant))
    ]


def test_a_profile_table_takes_writes_and_serves_one_tenant(pg: Any) -> None:
    ensure(pg, [MINI])
    store = PgVectorStore(pg, TABLE)
    store.write_and_swap("art_a", 1, mini_rows("art_a", 2, "t-a"))
    store.write_and_swap("art_b", 1, mini_rows("art_b", 3, "t-b"))
    pg.commit()

    with as_role(pg, "ingest_reader") as conn:
        hits = search_hybrid(
            conn, tenant_id="t-a", vector=[0.1] * 384, text="chunk", k=10, table=TABLE
        )
        assert {h["article_id"] for h in hits} == {"art_a"}
        conn.rollback()
        assert conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone() == (0,)  # noqa: S608


def test_retagging_moves_rows_to_another_tenant(pg: Any) -> None:
    ensure(pg, [MINI])
    store = PgVectorStore(pg, TABLE)
    store.write_and_swap("art_a", 1, mini_rows("art_a", 2, "t-a"))
    assert store.retag_tenant("art_a", "t-b") == 2
    assert store.retag_tenant("art_a", "t-b") == 0
    found = pg.execute(f"SELECT DISTINCT tenant_id, metadata->>'tenant_id' FROM {TABLE}")  # noqa: S608
    assert found.fetchall() == [("t-b", "t-b")]


def test_ensure_is_idempotent(pg: Any) -> None:
    assert ensure(pg, [MINI]) == [TABLE]
    PgVectorStore(pg, TABLE).write_and_swap("art_a", 1, mini_rows("art_a", 1, "t-a"))
    assert ensure(pg, [MINI]) == []
    assert pg.execute(f"SELECT count(*) FROM {TABLE}").fetchone() == (1,)  # noqa: S608


def test_a_changed_dimension_is_refused(pg: Any) -> None:
    ensure(pg, [MINI])
    with pytest.raises(ProfileTableError, match="384-d embeddings but profile mini_384 is 768-d"):
        ensure(pg, [replace(MINI, dimension=768)])
    bad = replace(parse(PROFILE_ENV).profiles["primary"], dimension=512)
    with pytest.raises(ProfileTableError, match="chunks_primary has 1024-d"):
        ensure(pg, [bad])
    assert dimension_of(pg, "chunks_nothing") is None
