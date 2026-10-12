"""Backend for the upload UI (docs/UI.md), behind CloudFront at /api/* through a function URL.

It is the UI's "static user": every call goes to the producer or control API signed as this
function's role. It exposes a fixed allow-list of routes, not a passthrough: no deletes,
patches, reindexing or tables. Upstream status codes and JSON bodies are returned unchanged.

Browsers resend cached basic-auth credentials on cross-site requests, so every POST must carry
X-Requested-With (which a cross-site form can't set without a CORS preflight that nothing
answers) and, when the browser sends Sec-Fetch-Site, come from the same origin.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Any
from urllib.parse import parse_qsl

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import LambdaFunctionUrlResolver, Response, content_types
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.utilities.typing import LambdaContext

from bff import upstream

logger = Logger()
app = LambdaFunctionUrlResolver()

PRODUCER = "PRODUCER_API_URL"
CONTROL = "CONTROL_API_URL"

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "entingestion-ui"
IDEMPOTENCY_HEADER = "idempotency-key"


# ---------------------------------------------------------------------------
# Routes (docs/UI.md "Routes"); anything else is a 404.
# ---------------------------------------------------------------------------


@app.get("/api/sources")
def list_sources() -> Response[str]:
    return _forward("GET", PRODUCER, "/v1/sources")


@app.get("/api/processes")
def list_processes() -> Response[str]:
    return _forward("GET", PRODUCER, "/v1/processes", query=True)


@app.post("/api/processes")
def create_process() -> Response[str]:
    return _forward("POST", PRODUCER, "/v1/processes")


@app.get("/api/processes/<process_id>")
def get_process(process_id: str) -> Response[str]:
    return _forward("GET", PRODUCER, "/v1/processes/{}", process_id)


@app.post("/api/processes/<process_id>/close")
def close_process(process_id: str) -> Response[str]:
    return _forward("POST", PRODUCER, "/v1/processes/{}/close", process_id)


@app.post("/api/processes/<process_id>/uploads")
def create_upload(process_id: str) -> Response[str]:
    return _forward("POST", PRODUCER, "/v1/processes/{}/uploads", process_id)


@app.get("/api/processes/<process_id>/uploads")
def list_uploads(process_id: str) -> Response[str]:
    return _forward("GET", PRODUCER, "/v1/processes/{}/uploads", process_id, query=True)


@app.get("/api/processes/<process_id>/summary")
def process_summary(process_id: str) -> Response[str]:
    return _forward("GET", CONTROL, "/v1/processes/{}/summary", process_id)


@app.get("/api/processes/<process_id>/articles")
def process_articles(process_id: str) -> Response[str]:
    return _forward("GET", CONTROL, "/v1/processes/{}/articles", process_id)


@app.get("/api/articles/<article_id>/status")
def article_status(article_id: str) -> Response[str]:
    return _forward("GET", CONTROL, "/v1/articles/{}/status", article_id)


# ---------------------------------------------------------------------------
# Forwarding
# ---------------------------------------------------------------------------


def _forward(method: str, api: str, template: str, *ids: str, query: bool = False) -> Response[str]:
    if method == "POST" and not _passes_csrf_guard():
        return _json(403, {"message": "forbidden"})
    if not all(ID_PATTERN.fullmatch(i) for i in ids):
        return _json(400, {"message": "invalid id"})

    headers: dict[str, str] = {}
    body: bytes | None = None
    if method == "POST":
        body = _body_bytes()
        headers["content-type"] = content_types.APPLICATION_JSON
        key = _header(IDEMPOTENCY_HEADER)
        if key:
            headers[IDEMPOTENCY_HEADER] = key

    started = time.monotonic()
    log = {"method": method, "upstream": api.removesuffix("_API_URL").lower(), "route": template}
    try:
        result = upstream.call(
            method,
            os.environ[api],
            template.format(*ids),
            query=_query() if query else None,
            body=body,
            headers=headers,
        )
    except upstream.UpstreamError as exc:
        logger.warning("upstream unavailable", extra={**log, "error": str(exc)})
        return _json(502, {"message": "upstream unavailable"})

    logger.info(
        "forwarded",
        extra={**log, "status": result.status, "ms": round((time.monotonic() - started) * 1000)},
    )
    extra_headers = {"Retry-After": result.retry_after} if result.retry_after else {}
    return Response(
        status_code=result.status,
        content_type=content_types.APPLICATION_JSON,
        body=result.body.decode("utf-8", errors="replace"),
        headers={"Cache-Control": "no-store", **extra_headers},
    )


def _passes_csrf_guard() -> bool:
    if _header(CSRF_HEADER) != CSRF_VALUE:
        return False
    site = _header("sec-fetch-site")
    return site is None or site == "same-origin"


def _header(name: str) -> str | None:
    for key, value in (app.current_event.headers or {}).items():
        if key.lower() == name:
            return str(value)
    return None


def _query() -> list[tuple[str, str]]:
    raw = app.current_event.raw_query_string
    if raw:
        return parse_qsl(raw, keep_blank_values=True)
    return list((app.current_event.query_string_parameters or {}).items())


def _body_bytes() -> bytes:
    raw = app.current_event.body or ""
    if app.current_event.is_base64_encoded:
        return base64.b64decode(raw)
    return raw.encode("utf-8")


def _json(status: int, body: dict[str, Any]) -> Response[str]:
    return Response(
        status_code=status,
        content_type=content_types.APPLICATION_JSON,
        body=json.dumps(body, separators=(",", ":")),
        headers={"Cache-Control": "no-store"},
    )


@app.not_found
def _on_not_found(_: Exception) -> Response[str]:
    return _json(404, {"message": "route not found"})


@app.exception_handler(Exception)  # type: ignore[untyped-decorator]
def _on_unexpected(exc: Exception) -> Response[str]:
    logger.exception("unhandled error", exc_info=exc)
    return _json(500, {"message": "internal server error"})


@logger.inject_lambda_context(correlation_id_path=correlation_paths.LAMBDA_FUNCTION_URL)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)
