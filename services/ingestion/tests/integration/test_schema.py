from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ingest.embedding.profiles import parse
from ingest.migrate import active_table, prepare
from ingest.vectorstore.migrations import MigrationError, apply
from tests.conftest import PROFILE_ENV

pytestmark = pytest.mark.integration

MIGRATIONS = Path(__file__).parents[2] / "db" / "migrations"


def insert_chunk(conn: Any, chunk_id: str, version: int, active: bool) -> None:
    conn.execute(
        "INSERT INTO chunks_primary (chunk_id, article_id, doc_version, ordinal, is_active,"
        " tenant_id, text, embed_text, token_count, embedding, embedding_model, pipeline_version)"
        " VALUES (%s, 'art_1', %s, 0, %s, 'default', 'hello world', 'T > hello world', 2,"
        " %s::halfvec, 'amazon.titan-embed-text-v2:0', 'v1')",
        (chunk_id, version, active, "[" + ",".join(["0.01"] * 1024) + "]"),
    )


def test_migrations_are_idempotent_and_recorded(pg: Any) -> None:
    assert apply(pg, MIGRATIONS) == []  # already applied by the fixture
    rows = pg.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    assert [r[0] for r in rows] == sorted(f.name.split("_", 1)[0] for f in MIGRATIONS.glob("*.sql"))


def test_prepare_is_idempotent_and_serves_the_default_table(pg: Any) -> None:
    profiles = parse(PROFILE_ENV).profiles.values()
    again = prepare(pg, MIGRATIONS, profiles, "chunks_primary")
    assert again == {"applied": [], "profile_tables_created": [], "active_table": "chunks_primary"}
    tables = pg.execute("SELECT table_name, dimension FROM profile_tables ORDER BY 1").fetchall()
    assert tables == [("chunks_compact", 512), ("chunks_primary", 1024)]
    view = pg.execute(
        "SELECT c.reloptions, v.viewowner FROM pg_class c"
        " JOIN pg_views v ON v.viewname = c.relname WHERE c.relname = 'chunks_active'"
    ).fetchone()
    assert view == (["security_invoker=true"], "ingest_views")
    grants = pg.execute(
        "SELECT grantee FROM information_schema.role_table_grants"
        " WHERE table_name = 'chunks_active' AND privilege_type = 'SELECT'"
        " AND grantee LIKE 'ingest_%'"
    ).fetchall()
    assert {g[0] for g in grants} == {"ingest_reader", "ingest_writer", "ingest_views"}


def test_prepare_follows_the_registry_on_a_rebuilt_database(pg: Any) -> None:
    # A fresh database serves the default table; the registry (DynamoDB) says compact is live.
    profiles = parse(PROFILE_ENV).profiles.values()
    out = prepare(pg, MIGRATIONS, profiles, "chunks_primary", registry_live="chunks_compact")
    assert out["active_table"] == "chunks_compact"
    assert active_table(pg) == "chunks_compact"


def test_prepare_refuses_a_live_table_without_a_profile(pg: Any) -> None:
    profiles = parse(PROFILE_ENV).profiles.values()
    with pytest.raises(ValueError, match="no embedding profile"):
        prepare(pg, MIGRATIONS, profiles, "chunks_primary", registry_live="chunks_gone")
    assert active_table(pg) == "chunks_primary"  # unchanged


def test_prepare_keeps_the_table_the_view_serves(pg: Any) -> None:
    from ingest.migrate import activate

    activate(pg, "chunks_compact")
    profiles = parse(PROFILE_ENV).profiles.values()
    assert prepare(pg, MIGRATIONS, profiles, "chunks_primary")["active_table"] == "chunks_compact"
    assert active_table(pg) == "chunks_compact"


def test_pgvector_version_and_roles(pg: Any) -> None:
    version = pg.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
    assert tuple(int(p) for p in version[0].split(".")) >= (0, 8, 0)
    roles = {r[0] for r in pg.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE 'ingest_%'")}
    assert {"ingest_writer", "ingest_reader", "ingest_ops", "ingest_views"} <= roles


def test_active_view_and_generated_tsvector(pg: Any) -> None:
    insert_chunk(pg, "c1", 1, active=False)
    insert_chunk(pg, "c2", 2, active=True)

    active = pg.execute("SELECT chunk_id, content_tsv::text FROM chunks_active").fetchall()

    assert [r[0] for r in active] == ["c2"]
    assert "'hello'" in active[0][1]


def test_vector_search_uses_halfvec_cosine(pg: Any) -> None:
    insert_chunk(pg, "c1", 1, active=True)
    pg.execute("SET hnsw.iterative_scan = relaxed_order")
    row = pg.execute(
        "SELECT chunk_id, embedding <=> %s::halfvec AS distance FROM chunks_active"
        " ORDER BY distance LIMIT 1",
        ("[" + ",".join(["0.01"] * 1024) + "]",),
    ).fetchone()
    assert row[0] == "c1"
    assert row[1] == pytest.approx(0.0, abs=1e-3)


def test_changed_migration_is_refused(pg: Any, tmp_path: Path) -> None:
    for path in MIGRATIONS.glob("*.sql"):
        (tmp_path / path.name).write_text(path.read_text() + "\n-- edited\n")
    with pytest.raises(MigrationError, match="changed after it was applied"):
        apply(pg, tmp_path)
