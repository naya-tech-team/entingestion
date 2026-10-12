"""PostgreSQL connections through RDS Proxy with IAM auth (no stored passwords)."""

from __future__ import annotations

import boto3
import psycopg

from ingest.config import Settings

_CONN: psycopg.Connection[tuple[object, ...]] | None = None
STATEMENT_TIMEOUT_MS = 30_000
# IAM token checks on a small burstable instance can take several seconds, mostly when cold.
CONNECT_TIMEOUT_S = 30


def _connect(settings: Settings) -> psycopg.Connection[tuple[object, ...]]:
    token = boto3.client("rds").generate_db_auth_token(
        DBHostname=settings.db_host, Port=settings.db_port, DBUsername=settings.db_user
    )
    return psycopg.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=token,
        sslmode=settings.db_sslmode,
        sslrootcert=settings.db_sslrootcert if settings.db_sslmode == "verify-full" else None,
        connect_timeout=CONNECT_TIMEOUT_S,
        options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
        application_name="ingest",
        # No server-side prepared statements: RDS Proxy would pin the connection.
        prepare_threshold=None,
    )


def connection(settings: Settings) -> psycopg.Connection[tuple[object, ...]]:
    """A connection reused across warm invocations; reconnects if it broke."""
    global _CONN  # noqa: PLW0603 - module-level connection cache for warm Lambdas
    if _CONN is None or _CONN.closed or _CONN.broken:
        _CONN = _connect(settings)
    return _CONN
