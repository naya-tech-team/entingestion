"""PostgreSQL connections through RDS Proxy with IAM auth (no stored passwords)."""

from __future__ import annotations

import boto3
import psycopg

from ingest.config import Settings

_CONN: psycopg.Connection[tuple[object, ...]] | None = None


def _connect(settings: Settings) -> psycopg.Connection[tuple[object, ...]]:
    token = boto3.client("rds").generate_db_auth_token(
        DBHostname=settings.db.host, Port=settings.db.port, DBUsername=settings.db.user
    )
    return psycopg.connect(
        host=settings.db.host,
        port=settings.db.port,
        dbname=settings.db.name,
        user=settings.db.user,
        password=token,
        sslmode=settings.db.sslmode,
        sslrootcert=settings.db.sslrootcert if settings.db.sslmode == "verify-full" else None,
        connect_timeout=settings.db.connect_timeout_seconds,
        options=f"-c statement_timeout={settings.db.statement_timeout_ms}",
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
