"""Migration 004 against a real pgvector database: ingest_ops reads the vector tables and
switches chunks_active (as the view's owner, ingest_views) but can't write or truncate, and a
master user without superuser rights (as on RDS/Aurora) can apply it."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from ingest.migrate import activate, status
from ingest.vectorstore.migrations import apply

pytestmark = pytest.mark.integration

MIGRATIONS = Path(__file__).parents[2] / "db" / "migrations"


def view_owner(conn: Any) -> str:
    row = conn.execute("SELECT viewowner FROM pg_views WHERE viewname = 'chunks_active'").fetchone()
    return str(row[0])


def test_view_belongs_to_ingest_views(pg: Any) -> None:
    assert view_owner(pg) == "ingest_views"


def test_ingest_ops_reads_status_and_switches_the_view(pg: Any) -> None:
    pg.execute("SET SESSION AUTHORIZATION ingest_ops")

    before = status(pg)
    assert before["active_table"] == "chunks_primary"
    assert before["tables"]["chunks_compact"] == {
        "rows": 0,
        "active_rows": 0,
        "active_articles": 0,
    }

    out = activate(pg, "chunks_compact")
    assert out == {"previous": "chunks_primary", "active_table": "chunks_compact"}
    assert status(pg)["active_table"] == "chunks_compact"
    assert view_owner(pg) == "ingest_views"  # the switch keeps the owner, so it can switch back

    activate(pg, "chunks_primary")  # roll back
    assert status(pg)["active_table"] == "chunks_primary"

    # Readers still see the recreated view.
    pg.execute("RESET SESSION AUTHORIZATION")
    pg.execute("SET SESSION AUTHORIZATION ingest_reader")
    assert pg.execute("SELECT count(*) FROM chunks_active").fetchone()[0] == 0
    pg.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "TRUNCATE chunks_primary",
        "DELETE FROM chunks_primary",
        "DROP TABLE chunks_compact",
        "DELETE FROM article_tombstones",
    ],
)
def test_ingest_ops_cannot_change_data(pg: Any, statement: str) -> None:
    pg.execute("SET SESSION AUTHORIZATION ingest_ops")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        pg.execute(statement)
    pg.rollback()


def test_a_non_superuser_master_applies_every_migration(pg_url: str) -> None:
    """RDS/Aurora's master user has CREATEROLE but isn't a superuser: it must be able to create
    ingest_views and ingest_ops, hand the view to ingest_views, and still switch it."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    master = f"m_{uuid.uuid4().hex[:8]}"
    db_url = pg_url.rsplit("/", 1)[0] + f"/{name}"
    with psycopg.connect(pg_url, autocommit=True) as admin:
        try:  # start from no ingest_ops/ingest_views, as on a new cluster
            admin.execute("DROP ROLE IF EXISTS ingest_ops, ingest_views")
        except psycopg.errors.DependentObjectsStillExist:
            pytest.skip("another database still uses ingest_ops/ingest_views")
        admin.execute(f"CREATE ROLE {master} LOGIN CREATEROLE")
        admin.execute(f'CREATE DATABASE "{name}" OWNER {master}')
    try:
        with psycopg.connect(db_url) as conn:
            conn.execute("CREATE EXTENSION vector")  # rds_superuser may; done as postgres here
            conn.commit()
            conn.execute(f"SET SESSION AUTHORIZATION {master}")
            conn.commit()
            assert "004" in apply(conn, MIGRATIONS)
            conn.autocommit = False
            assert view_owner(conn) == "ingest_views"
            assert activate(conn, "chunks_compact")["active_table"] == "chunks_compact"
    finally:
        with psycopg.connect(pg_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            # Roles granted by the test master go with it; later tests recreate them as postgres.
            admin.execute("DROP ROLE IF EXISTS ingest_ops, ingest_views")
            admin.execute(f"DROP ROLE IF EXISTS {master}")
