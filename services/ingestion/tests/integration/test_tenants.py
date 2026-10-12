"""Migration 005 against a real pgvector database, as the roles the functions use (row-level
security doesn't apply to superusers or table owners, so every check switches role)."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest

from ingest.migrate import activate, status
from ingest.query import all_tenants, counts, versions
from ingest.vectorstore.index import ArticleAttributes, PgVectorStore
from ingest.vectorstore.migrations import apply
from tests.integration.test_index import rows

pytestmark = pytest.mark.integration

MIGRATIONS = Path(__file__).parents[2] / "db" / "migrations"
TABLE = "chunks_primary"


@contextmanager
def as_role(conn: Any, role: str) -> Iterator[Any]:
    """Act as `role` in transactions (the fixture's connection is in autocommit, where a
    SET LOCAL would end with its own statement)."""
    conn.autocommit = False
    conn.execute(f"SET SESSION AUTHORIZATION {role}")
    conn.commit()  # survives the rollbacks below
    try:
        yield conn
    finally:
        conn.rollback()
        conn.execute("RESET SESSION AUTHORIZATION")
        conn.commit()
        conn.autocommit = True


def seed(pg: Any) -> None:
    store = PgVectorStore(pg, TABLE)
    store.write_and_swap("art_a", 1, rows("art_a", 1, 2, tenant="t-a"))
    store.write_and_swap("art_b", 1, rows("art_b", 1, 3, tenant="t-b"))
    pg.commit()


def visible(conn: Any, relation: str = "chunks_active") -> set[str]:
    return {r[0] for r in conn.execute(f"SELECT DISTINCT tenant_id FROM {relation}")}  # noqa: S608


def test_a_reader_sees_only_its_tenant(pg: Any) -> None:
    seed(pg)
    with as_role(pg, "ingest_reader") as conn:
        assert visible(conn) == set()  # no tenant set: nothing (fail closed)
        assert visible(conn, TABLE) == set()
        conn.execute("SELECT set_config('app.tenant_id', 't-a', true)")
        assert visible(conn) == {"t-a"}  # through the view: checked as the reader
        assert visible(conn, TABLE) == {"t-a"}
        conn.rollback()
        assert visible(conn) == set()  # SET LOCAL ended with the transaction


def test_internal_actions_see_every_tenant(pg: Any) -> None:
    seed(pg)
    with as_role(pg, "ingest_reader") as conn:
        all_tenants(conn)
        assert visible(conn) == {"t-a", "t-b"}
        assert counts(conn, None) == {"art_a": 2, "art_b": 3}
        assert versions(conn, TABLE, "", 10)["rows"] == [["art_a", 1, 2], ["art_b", 1, 3]]


def test_ops_status_and_the_switch_cover_every_tenant(pg: Any) -> None:
    seed(pg)
    with as_role(pg, "ingest_ops") as conn:
        assert status(conn)["tables"][TABLE]["active_rows"] == 5
        activate(conn, "chunks_compact")
        activate(conn, TABLE)
    # The recreated view still checks the caller.
    with as_role(pg, "ingest_reader") as conn:
        assert visible(conn) == set()
        conn.execute("SELECT set_config('app.tenant_id', 't-b', true)")
        assert visible(conn) == {"t-b"}


def test_the_writer_writes_and_updates_every_tenant(pg: Any) -> None:
    with as_role(pg, "ingest_writer") as conn:
        store = PgVectorStore(conn, TABLE)
        store.write_and_swap("art_a", 1, rows("art_a", 1, 2, tenant="t-a"))
        store.write_and_swap("art_b", 1, rows("art_b", 1, 1, tenant="t-b"))
        moved = ArticleAttributes("t-c", "wire", "2026-09-30T10:00:00Z", {"lang": "de"})
        assert store.update_metadata("art_a", moved) == 2
        got = conn.execute(
            f"SELECT DISTINCT tenant_id, source, published_at::text, metadata->'article'->>'lang',"  # noqa: S608
            f" metadata->>'n' IS NOT NULL FROM {TABLE} WHERE article_id = 'art_a'"
        ).fetchall()
        assert got == [("t-c", "wire", "2026-09-30", "de", True)]
        assert store.delete_article("art_b", 1) == 1
        conn.commit()


def test_an_empty_tenant_is_refused(pg: Any) -> None:
    bad = rows("art_x", 1, 1, tenant="")
    store = PgVectorStore(pg, TABLE)
    store.write_and_swap("art_x", 1, bad)  # Index writes 'default' for a row without a tenant
    tenant = pg.execute(f"SELECT tenant_id FROM {TABLE} WHERE article_id = 'art_x'")  # noqa: S608
    assert tenant.fetchone()[0] == "default"
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(f"UPDATE {TABLE} SET tenant_id = '' WHERE article_id = 'art_x'")  # noqa: S608
    pg.rollback()


def test_migration_005_backfills_existing_rows(pg_url: str, tmp_path: Path) -> None:
    """Rows written before 005 (no tenant; filters only in metadata) get 'default' and typed
    columns, and the view keeps serving the table it served."""
    for path in MIGRATIONS.glob("00[1-4]_*.sql"):
        shutil.copy(path, tmp_path / path.name)
    name = f"t_{uuid.uuid4().hex[:12]}"
    db_url = pg_url.rsplit("/", 1)[0] + f"/{name}"
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(db_url) as conn:
            apply(conn, tmp_path)
            conn.autocommit = False
            conn.execute(
                "INSERT INTO chunks_compact (chunk_id, article_id, doc_version, ordinal,"
                " is_active, text, embed_text, token_count, metadata, embedding, embedding_model,"
                " pipeline_version) VALUES ('c1', 'art_1', 1, 0, true, 'old words', 'old words',"
                " 2, %s::jsonb, %s::halfvec, 'm', 'v1')",
                (
                    '{"source": "news", "published_at": "2026-02-03T04:05:06Z"}',
                    "[" + ",".join(["0.01"] * 512) + "]",
                ),
            )
            activate(conn, "chunks_compact")
            assert apply(conn, MIGRATIONS) == ["005", *(m.stem[:3] for m in _after_005())]
            conn.autocommit = False
            row = conn.execute(
                "SELECT tenant_id, source, published_at::text FROM chunks_active"
            ).fetchone()
            assert row == ("default", "news", "2026-02-03")
            options = conn.execute(
                "SELECT reloptions FROM pg_class WHERE relname = 'chunks_active'"
            ).fetchone()[0]
            assert "security_invoker=true" in options
            assert status(conn)["active_table"] == "chunks_compact"
    finally:
        with psycopg.connect(pg_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _after_005() -> list[Path]:
    return sorted(p for p in MIGRATIONS.glob("*.sql") if p.name[:3] > "005")
