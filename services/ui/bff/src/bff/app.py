"""Backend for the upload UI (docs/UI.md), behind CloudFront at /api/* through a function URL.

It is the UI's "static user": every call goes to the producer or control API signed as this
function's role. It exposes a fixed allow-list of routes, not a passthrough: no deletes,
patches, reindexing or table changes. Upstream status codes and JSON bodies are returned
unchanged.

The Operations routes (/api/ops/*) add read-only views (overview, articles by status, the
table registry, search) and three actions: re-run articles, re-run a process's failures and
redrive the DLQ. The BFF caps the actions beyond the control API's own validation (at most
MAX_RERUN_IDS article ids, no from_stage, a redrive max of at most MAX_REDRIVE; bff.config)
and logs each one's route, dry_run and count, never its body.

Browsers resend cached basic-auth credentials on cross-site requests, so every POST must carry
X-Requested-With (which a cross-site form can't set without a CORS preflight that nothing
answers) and, when the browser sends Sec-Fetch-Site, come from the same origin.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import parse_qsl

import urllib3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import LambdaFunctionUrlResolver, Response, content_types
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.utilities.typing import LambdaContext

from bff import upstream
from bff.config import Settings, get_settings

logger = Logger()
app = LambdaFunctionUrlResolver()

Api = Literal["producer", "control"]
PRODUCER: Api = "producer"
CONTROL: Api = "control"

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "entingestion-ui"
IDEMPOTENCY_HEADER = "idempotency-key"

Errors = list[dict[str, Any]]


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


# -- Operations ---------------------------------------------------------------------------


@app.get("/api/ops/overview")
def ops_overview() -> Response[str]:
    return _forward("GET", CONTROL, "/v1/ops/overview")


@app.get("/api/ops/articles")
def ops_articles() -> Response[str]:
    return _forward("GET", CONTROL, "/v1/articles", query=True)


@app.get("/api/ops/tables")
def ops_tables() -> Response[str]:
    return _forward("GET", CONTROL, "/v1/tables", query=True)


@app.post("/api/ops/search")
def ops_search() -> Response[str]:
    return _forward("POST", CONTROL, "/v1/search")


@app.post("/api/ops/ingest")
def ops_ingest() -> Response[str]:
    return _ops_action("/v1/ingest", check=_check_rerun)


@app.post("/api/ops/processes/<process_id>/ingest")
def ops_process_ingest(process_id: str) -> Response[str]:
    return _ops_action("/v1/processes/{}/ingest", process_id)


@app.post("/api/ops/dlq/redrive")
def ops_redrive() -> Response[str]:
    return _ops_action("/v1/dlq/redrive", check=_check_redrive)


def _check_rerun(body: dict[str, Any]) -> Errors:
    errors: Errors = []
    ids = body.get("article_ids")
    cap = get_settings().max_rerun_ids
    if isinstance(ids, list) and len(ids) > cap:
        errors.append(
            {"loc": ["body", "article_ids"], "msg": f"at most {cap} article ids from the UI"}
        )
    if "from_stage" in body:
        errors.append(
            {
                "loc": ["body", "from_stage"],
                "msg": "not allowed from the UI: re-runs start from the beginning",
            }
        )
    return errors


def _check_redrive(body: dict[str, Any]) -> Errors:
    value = body.get("max")
    cap = get_settings().max_redrive
    if "max" in body and (not isinstance(value, int) or isinstance(value, bool) or value > cap):
        return [{"loc": ["body", "max"], "msg": f"an integer of at most {cap} from the UI"}]
    return []


def _ops_action(
    template: str, *ids: str, check: Callable[[dict[str, Any]], Errors] | None = None
) -> Response[str]:
    """A control-API action: guarded like any POST, then the body is checked against the UI's
    caps before it's forwarded byte for byte. Logs route, dry_run and count, not the body."""
    rejected = _rejected("POST", ids)
    if rejected is not None:
        return rejected
    try:
        raw = _body_bytes().decode("utf-8")
        body = json.loads(raw) if raw.strip() else {}
    except ValueError:  # JSONDecodeError and UnicodeDecodeError
        return _json(400, {"message": "invalid JSON"})
    if not isinstance(body, dict):
        return _invalid([{"loc": ["body"], "msg": "must be a JSON object"}])
    errors = check(body) if check else []
    if errors:
        return _invalid(errors)

    response = _forward("POST", CONTROL, template, *ids)
    ids_in_body = body.get("article_ids")
    logger.info(
        "ops action",
        extra={
            "route": template,
            "path_ids": list(ids),
            "dry_run": body.get("dry_run") is True,
            "count": len(ids_in_body) if isinstance(ids_in_body, list) else None,
            "status": response.status_code,
        },
    )
    return response


def _invalid(errors: Errors) -> Response[str]:
    """422 in the shape the APIs use for request validation."""
    return _json(422, {"message": "request validation failed", "errors": errors})


# ---------------------------------------------------------------------------
# Forwarding
# ---------------------------------------------------------------------------


def _rejected(method: str, ids: tuple[str, ...]) -> Response[str] | None:
    """The CSRF guard on POSTs, then the path ids; None when the request may go on."""
    if method == "POST" and not _passes_csrf_guard():
        return _json(403, {"message": "forbidden"})
    if not all(ID_PATTERN.fullmatch(i) for i in ids):
        return _json(400, {"message": "invalid id"})
    return None


def _base_url(settings: Settings, api: Api) -> str:
    return settings.producer_api_url if api == PRODUCER else settings.control_api_url


def _forward(method: str, api: Api, template: str, *ids: str, query: bool = False) -> Response[str]:
    rejected = _rejected(method, ids)
    if rejected is not None:
        return rejected

    headers: dict[str, str] = {}
    body: bytes | None = None
    if method == "POST":
        body = _body_bytes()
        headers["content-type"] = content_types.APPLICATION_JSON
        key = _header(IDEMPOTENCY_HEADER)
        if key:
            headers[IDEMPOTENCY_HEADER] = key

    settings = get_settings()
    started = time.monotonic()
    log = {"method": method, "upstream": api, "route": template}
    try:
        result = upstream.call(
            method,
            _base_url(settings, api),
            template.format(*ids),
            timeout=urllib3.Timeout(connect=settings.connect_timeout, read=settings.read_timeout),
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
    get_settings()  # cached: missing configuration fails the first invocation, not a route
    return app.resolve(event, context)
