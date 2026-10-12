"""tables Lambda: the vector-table operations behind the control API's /v1/tables routes
(brief §7.3), from inside the VPC as ingest_ops (IAM auth, no master credentials):

    {"action": "status"}                                 live table + row counts per table
    {"action": "activate", "table": "chunks_titan2_512"} switch chunks_active (also rollback)

ingest_ops can read the vector tables and act as the view's owner (ingest_views, migration 004);
it can't write rows or change the schema. Migrations and truncate stay in the migrate function.
"""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

from ingest.config import get_settings
from ingest.migrate import activate, known_table, status
from ingest.vectorstore.db import connection

logger = Logger()


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    action = event.get("action")
    if action not in ("status", "activate"):
        raise ValueError(f"unknown action {action!r}; expected status or activate")
    table = known_table(event) if action == "activate" else ""
    conn = connection(get_settings())
    conn.rollback()  # a warm connection starts clean
    try:
        if action == "status":
            return status(conn)
        return activate(conn, table)
    finally:
        conn.rollback()  # never leave a transaction open on the cached connection
