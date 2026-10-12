"""migrate Lambda: apply db/migrations in order (brief §8.5). Invoked by CI after apply:

    aws lambda invoke --function-name <prefix>-migrate /dev/stdout

Runs as the cluster's master user (credentials from Secrets Manager), directly against the
writer endpoint: the first migration creates the extension and the IAM-auth users that every
other function connects as through RDS Proxy. After the numbered migrations it creates the table
of every embedding profile that has none yet (ingest.vectorstore.profile_tables).

The same function empties a vector table for the rebuild test (UC10), unless ALLOW_TRUNCATE is
false (prod):

    {"action": "truncate", "table": "...", "confirm": "..."}   confirm == table

The vector-table status and switch (brief §7.3) run in the tables function as ingest_ops
(ingest.tables); status(), activate() and active_table() here are shared with it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import boto3
import psycopg
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from psycopg import sql

from ingest.config import get_settings
from ingest.vectorstore import migrations, profile_tables
from ingest.vectorstore.targets import TARGETS

logger = Logger()

_HERE = Path(__file__).resolve()
# Owns chunks_active (migration 004); the master user and ingest_ops are members.
VIEW_OWNER = "ingest_views"


def migrations_dir() -> Path:
    configured = os.environ.get("MIGRATIONS_DIR")
    if configured:
        return Path(configured)
    for base in (_HERE.parents[1], _HERE.parents[2]):  # Lambda zip root, or the repo checkout
        candidate = base / "db" / "migrations"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("db/migrations not found; set MIGRATIONS_DIR")


def ca_bundle() -> str:
    return os.environ.get("DB_CA_BUNDLE", str(_HERE.parents[1] / "certs" / "rds-global-bundle.pem"))


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    settings = get_settings()
    action = event.get("action", "migrate")
    if action in ("status", "activate"):
        raise ValueError(f"{action!r} runs in the tables function (ingest.tables), as ingest_ops")
    if action not in ("migrate", "truncate"):
        raise ValueError(f"unknown action {action!r}")
    table = known_table(event) if action == "truncate" else ""
    if action == "truncate":
        if event.get("confirm") != table:
            raise ValueError("truncate needs confirm equal to the table name")
        if not settings.allow_truncate:
            raise PermissionError("truncate is switched off here (ALLOW_TRUNCATE=false)")

    settings.require("db_direct_host", "db_secret_arn", "db_name")
    secret = json.loads(
        boto3.client("secretsmanager").get_secret_value(SecretId=settings.db_secret_arn)[
            "SecretString"
        ]
    )
    with psycopg.connect(
        host=settings.db_direct_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=secret["username"],
        password=secret["password"],
        sslmode="verify-full",
        sslrootcert=ca_bundle(),
        connect_timeout=10,
        application_name="ingest-migrate",
    ) as conn:
        if action == "migrate":
            applied = migrations.apply(conn, migrations_dir())
            logger.info("migrations applied", extra={"applied": applied})
            # Then a table for every embedding profile that has none yet.
            created = profile_tables.ensure(conn, settings.embedding_profiles.values())
            if created:
                logger.warning("profile tables created", extra={"tables": created})
            return {"applied": applied, "profile_tables_created": created}
        conn.execute(sql.SQL("TRUNCATE {t}").format(t=sql.Identifier(table)))
        conn.commit()
        logger.warning("table truncated", extra={"table": table})
        return {"truncated": table}


def known_table(event: dict[str, Any]) -> str:
    table = str(event.get("table", ""))
    if table not in TARGETS:
        raise ValueError(f"unknown table {table!r}; known: {sorted(TARGETS)}")
    return table


def active_table(conn: psycopg.Connection[Any]) -> str | None:
    row = conn.execute(
        "SELECT definition FROM pg_views WHERE schemaname = 'public' AND viewname = 'chunks_active'"
    ).fetchone()
    match = re.search(r"FROM\s+(?:public\.)?(\w+)", str(row[0])) if row else None
    return match.group(1) if match else None


def status(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for table in TARGETS:
        if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None:  # type: ignore[index]
            tables[table] = None  # its migration hasn't run
            continue
        rows, active, articles = conn.execute(  # type: ignore[misc]
            sql.SQL(
                "SELECT count(*), count(*) FILTER (WHERE is_active),"
                " count(DISTINCT article_id) FILTER (WHERE is_active) FROM {t}"
            ).format(t=sql.Identifier(table))
        ).fetchone()
        tables[table] = {"rows": rows, "active_rows": active, "active_articles": articles}
    conn.rollback()
    return {"active_table": active_table(conn), "tables": tables}


def activate(conn: psycopg.Connection[Any], table: str) -> dict[str, Any]:
    """Point chunks_active at `table`. The vector column's type differs per table, so the view is
    dropped and recreated in one transaction: readers wait for the lock, never see it missing.

    Runs as the view's owner (SET LOCAL ROLE, so it ends with the transaction): the caller only
    needs membership in ingest_views, and the new view keeps that owner. security_invoker keeps
    row-level security (migration 005) the caller's: readers see their tenant only."""
    previous = active_table(conn)
    with conn.transaction():
        conn.execute(sql.SQL("SET LOCAL ROLE {r}").format(r=sql.Identifier(VIEW_OWNER)))
        conn.execute("DROP VIEW IF EXISTS chunks_active")
        conn.execute(
            sql.SQL(
                "CREATE VIEW chunks_active WITH (security_invoker = true)"
                " AS SELECT * FROM {t} WHERE is_active"
            ).format(t=sql.Identifier(table))
        )
        conn.execute("GRANT SELECT ON chunks_active TO ingest_reader, ingest_writer")
    # The read above opened a transaction (unless autocommit), which made the block a savepoint.
    conn.commit()
    logger.warning("chunks_active switched", extra={"from": previous, "to": table})
    return {"previous": previous, "active_table": table}
