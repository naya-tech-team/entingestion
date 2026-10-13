"""Control API handler. API Gateway authorizes every call with IAM; the invoke policies split
readers (GET), searchers (POST /v1/search) and operators (everything).

The routes live in one Powertools Router per area (ingest.control_api.routes):

    runs     POST /v1/ingest, POST /v1/processes/{id}/ingest, POST /v1/dlq/redrive
    tables   POST /v1/reindex, GET /v1/tables, POST /v1/tables/active,
             DELETE /v1/tables/writes/{table}
    reads    GET /v1/articles, GET /v1/articles/{id}/status, GET /v1/processes/{id}/summary,
             GET /v1/processes/{id}/articles, POST /v1/search
    ops      GET /v1/ops/overview

This module keeps the resolver, the error handlers and the Lambda handler.
"""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.utilities.typing import LambdaContext

from ingest.config import require
from ingest.control_api.http import BadRequest, json_response
from ingest.control_api.routes import ops, reads, runs, tables

logger = Logger()
tracer = Tracer()
metrics = Metrics()
app = APIGatewayRestResolver()
for area in (runs, tables, reads, ops):
    app.include_router(area.router)

# What every route needs; a route's own needs (queues, functions, the access policy) are checked
# by the code that uses them.
REQUIRED = ("status_table", "staging_bucket", "producer_articles_table", "producer_process_table")


@app.exception_handler(BadRequest)  # type: ignore[untyped-decorator]
def _on_bad_request(err: BadRequest) -> Response[str]:
    return json_response(422, {"message": "request validation failed", "errors": err.errors})


@app.not_found
def _on_not_found(_: Exception) -> Response[str]:
    return json_response(404, {"message": "route not found"})


@app.exception_handler(Exception)  # type: ignore[untyped-decorator]
def _on_unexpected(err: Exception) -> Response[str]:
    logger.exception("unhandled error")
    return json_response(500, {"message": "internal error"})


@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_REST)
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    require(*REQUIRED)
    return app.resolve(event, context)
