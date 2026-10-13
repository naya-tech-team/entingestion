"""Postgres + pgvector via testcontainers, or an existing server from INGEST_TEST_PG_URL.
Skipped when neither is available."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).parents[2] / "db" / "migrations"
IMAGE = "pgvector/pgvector:pg16"


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
    except Exception:  # any failure means "no usable Docker"
        return False
    return True


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    if url := os.environ.get("INGEST_TEST_PG_URL"):  # a server with pgvector >= 0.7 (halfvec)
        yield url
        return
    if not _docker_available():
        pytest.skip("Docker isn't available; integration tests need testcontainers")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(IMAGE, driver=None) as container:
        yield container.get_connection_url()


@pytest.fixture
def pg(pg_url: str) -> Iterator[object]:
    """A fresh database per test, prepared as the migrate function does: every migration, the
    test profiles' tables (chunks_primary, chunks_compact), chunks_active on chunks_primary."""
    import uuid

    import psycopg

    from ingest.embedding.profiles import parse
    from ingest.migrate import prepare
    from tests.conftest import PROFILE_ENV

    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = pg_url.rsplit("/", 1)[0] + f"/{name}"
    with psycopg.connect(url) as conn:
        config = parse(PROFILE_ENV)
        prepare(conn, MIGRATIONS, config.profiles.values(), config.profiles[config.default].table)
        yield conn
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
