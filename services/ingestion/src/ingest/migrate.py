"""migrate Lambda: bring the database to the current schema (`make migrate` invokes it after
an apply):

1. db/migrations in order (ingest.vectorstore.migrations): the extension, the IAM-auth users
   every other function connects as, and the tables and roles shared by all vector tables;
2. a vector table for every embedding profile that has none yet
   (ingest.vectorstore.profile_tables);
3. chunks_active recreated over the default registry's live table: the table it already serves
   (the switch keeps the two in step), or on a new database the default profile's table.

Runs as the cluster's master user (credentials from Secrets Manager), directly against the
writer endpoint.

The same function empties a vector table for the rebuild test (UC10), unless ALLOW_TRUNCATE is
false (prod):

    {"action": "truncate", "table": "...", "confirm": "..."}   confirm == table

The vector-table status and switch run in the tables function as ingest_ops (ingest.tables);
status(), activate() and active_table() here are shared with it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import boto3
import psycopg
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from psycopg import sql

from ingest import aws
from ingest.config import get_settings
from ingest.embedding.profiles import EmbeddingProfile
from ingest.vectorstore import migrations, profile_tables
from ingest.vectorstore import registry as vector_registry
from ingest.vectorstore.targets import TARGETS

logger = Logger()

_HERE = Path(__file__).resolve()
# Owns chunks_active (db/migrations/001_schema.sql); the master user and ingest_ops are members.
VIEW_OWNER = "ingest_views"
# migrate connects as the master user, straight to the writer endpoint.
REQUIRED = ("db.direct_host", "db.secret_arn", "db.name")


def migrations_dir() -> Path:
    configured = get_settings().db.migrations_dir
    if configured:
        return Path(configured)
    for base in (_HERE.parents[1], _HERE.parents[2]):  # Lambda zip root, or the repo checkout
        candidate = base / "db" / "migrations"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("db/migrations not found; set MIGRATIONS_DIR")


def ca_bundle() -> str:
    configured = get_settings().db.ca_bundle
    return configured or str(_HERE.parents[1] / "certs" / "rds-global-bundle.pem")


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

    settings.require(*REQUIRED)
    # The default registry's live table (DynamoDB, #CONFIG items only): chunks_active must serve
    # it, also on a rebuilt database. A failed read stops migrate rather than guess.
    registry_live = None
    if action == "migrate":
        settings.require("status_table")
        registry_live = vector_registry.recorded_live(aws.status_store())
    secret = json.loads(
        boto3.client("secretsmanager").get_secret_value(SecretId=settings.db.secret_arn)[
            "SecretString"
        ]
    )
    with psycopg.connect(
        host=settings.db.direct_host,
        port=settings.db.port,
        dbname=settings.db.name,
        user=secret["username"],
        password=secret["password"],
        sslmode="verify-full",
        sslrootcert=ca_bundle(),
        connect_timeout=10,
        application_name="ingest-migrate",
    ) as conn:
        if action == "migrate":
            return prepare(
                conn,
                migrations_dir(),
                settings.embedding.profiles.values(),
                settings.target_table,
                registry_live=registry_live,
            )
        conn.execute(sql.SQL("TRUNCATE {t}").format(t=sql.Identifier(table)))
        conn.commit()
        logger.warning("table truncated", extra={"table": table})
        return {"truncated": table}


def prepare(
    conn: psycopg.Connection[Any],
    directory: Path,
    profiles: Iterable[EmbeddingProfile],
    default_table: str,
    *,
    registry_live: str | None = None,
) -> dict[str, Any]:
    """Migrations, then a table per embedding profile, then chunks_active over the default
    registry's live table (registry_live). Before any change has recorded one: the table the view
    serves already, else default_table (the default profile's)."""
    profiles = list(profiles)
    applied = migrations.apply(conn, directory)
    logger.info("migrations applied", extra={"applied": applied})
    created = profile_tables.ensure(conn, profiles)
    if created:
        logger.warning("profile tables created", extra={"tables": created})
    live = registry_live or active_table(conn) or default_table
    if live not in {p.table for p in profiles}:
        raise ValueError(f"the live table {live!r} has no embedding profile; add it back first")
    activate(conn, live)
    return {"applied": applied, "profile_tables_created": created, "active_table": live}


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
            tables[table] = None  # not created yet: migrate creates it
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
    the tables' row-level security the caller's: readers see their tenant only."""
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
    if previous != table:
        logger.warning("chunks_active switched", extra={"from": previous, "to": table})
    return {"previous": previous, "active_table": table}
