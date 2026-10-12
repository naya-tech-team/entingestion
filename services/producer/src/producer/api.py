"""API Gateway (REST, {proxy+}) Lambda handler (brief §4)."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response, content_types
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.utilities.idempotency import (
    DynamoDBPersistenceLayer,
    IdempotencyConfig,
    idempotent_function,
)
from aws_lambda_powertools.utilities.idempotency.exceptions import (
    IdempotencyAlreadyInProgressError,
    IdempotencyValidationError,
)
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, ValidationError

from producer.config import get_settings
from producer.errors import FieldError, MalformedRequestError, ProducerError, RequestValidationError
from producer.models import (
    CloseProcessRequest,
    CreateProcessRequest,
    CreateUploadRequest,
    ListArticlesQuery,
    ListProcessesQuery,
    ListUploadsQuery,
    PatchArticleRequest,
)
from producer.repository import Repository
from producer.service import ProducerService
from producer.storage import Storage, make_s3_client

logger = Logger()
tracer = Tracer()
app = APIGatewayRestResolver()


@lru_cache(maxsize=1)
def get_service() -> ProducerService:
    settings = get_settings()
    repo = Repository.from_names(
        settings.process_table, settings.articles_table, settings.uploads_table
    )
    return ProducerService(settings, repo, Storage(make_s3_client(), settings.raw_bucket))


# Idempotency-Key on POSTs (create process, create upload, close): a retried request gets the
# first response instead of a duplicate. Keys are scoped to the caller and the route, and remembered
# for a day; reusing one with a different body is refused.
IDEMPOTENCY_HEADER = "idempotency-key"
_KEY_PATTERN = re.compile(r"^[\x21-\x7e]{1,128}$")
_IDEMPOTENCY = IdempotencyConfig(
    event_key_jmespath="key", payload_validation_jmespath="payload", expires_after_seconds=86400
)


@lru_cache(maxsize=1)
def _idempotency_store() -> DynamoDBPersistenceLayer:
    return DynamoDBPersistenceLayer(table_name=os.environ["IDEMPOTENCY_TABLE"])


def _once(route: str, produce: Callable[[], tuple[int, BaseModel]]) -> Response[str]:
    """Run produce() at most once per (caller, route, Idempotency-Key)."""
    key = (app.current_event.headers or {}).get(IDEMPOTENCY_HEADER) or next(
        (
            v
            for k, v in (app.current_event.headers or {}).items()
            if k.lower() == IDEMPOTENCY_HEADER
        ),
        None,
    )
    if key is None:
        status, model = produce()
        return _model(status, model)
    if not _KEY_PATTERN.match(key):
        raise RequestValidationError(
            "Request validation failed",
            [FieldError(("header", "Idempotency-Key"), "1-128 printable ASCII characters")],
        )

    @idempotent_function(  # type: ignore[untyped-decorator]
        data_keyword_argument="request", config=_IDEMPOTENCY, persistence_store=_idempotency_store()
    )
    def run(request: dict[str, str]) -> dict[str, Any]:
        status, model = produce()
        return {"status": status, "body": model.model_dump(mode="json")}

    try:
        out = run(
            request={"key": f"{_caller()}|{route}|{key}", "payload": app.current_event.body or ""}
        )
    except IdempotencyValidationError:
        return _json(422, _error_body("Idempotency-Key was used with a different request"))
    except IdempotencyAlreadyInProgressError:
        return _json(409, _error_body("a request with this Idempotency-Key is still in progress"))
    return _json(int(out["status"]), out["body"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json(status: int, body: Any) -> Response[str]:
    return Response(
        status_code=status,
        content_type=content_types.APPLICATION_JSON,
        body=json.dumps(body, separators=(",", ":")),
    )


def _model(status: int, model: BaseModel) -> Response[str]:
    return _json(status, model.model_dump(mode="json"))


def _error_body(message: str, errors: list[FieldError] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"message": message}
    if errors is not None:
        body["errors"] = [{"loc": list(e.loc), "msg": e.msg} for e in errors]
    return body


def _caller() -> str:
    identity = app.current_event.request_context.identity
    return identity.user_arn or identity.caller or "unknown"


def _body[ModelT: BaseModel](model: type[ModelT], *, optional: bool = False) -> ModelT:
    raw = app.current_event.body
    if raw is None or raw.strip() == "":
        if optional:
            return model.model_validate({})
        raise RequestValidationError(
            "Request validation failed", [FieldError(("body",), "request body is required")]
        )
    try:
        data = json.loads(app.current_event.decoded_body or "")
    except ValueError as exc:
        raise MalformedRequestError(
            "Malformed request", [FieldError(("body",), "body must be valid JSON")]
        ) from exc
    if not isinstance(data, dict):
        raise RequestValidationError(
            "Request validation failed", [FieldError(("body",), "body must be a JSON object")]
        )
    return _validate(model, data, "body")


def _validate[ModelT: BaseModel](model: type[ModelT], data: dict[str, Any], where: str) -> ModelT:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        errors = [
            FieldError(
                (where, *(str(p) if not isinstance(p, int) else p for p in e["loc"])), e["msg"]
            )
            for e in exc.errors()
        ]
        raise RequestValidationError("Request validation failed", errors) from exc


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@app.exception_handler(RequestValidationError)  # type: ignore[untyped-decorator]
def _on_validation(exc: RequestValidationError) -> Response[str]:
    return _json(exc.status_code, _error_body(exc.message, exc.errors))


@app.exception_handler(ProducerError)  # type: ignore[untyped-decorator]
def _on_domain_error(exc: ProducerError) -> Response[str]:
    return _json(exc.status_code, _error_body(str(exc)))


@app.not_found
def _on_not_found(_: Exception) -> Response[str]:
    return _json(404, _error_body("route not found"))


@app.exception_handler(Exception)  # type: ignore[untyped-decorator]
def _on_unexpected(exc: Exception) -> Response[str]:
    logger.exception("unhandled error", exc_info=exc)
    return _json(500, _error_body("internal server error"))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/v1/sources")
@tracer.capture_method
def list_sources() -> Response[str]:
    return _model(200, get_service().list_sources(_caller()))


@app.post("/v1/processes")
@tracer.capture_method
def create_process() -> Response[str]:
    req = _body(CreateProcessRequest)

    def create() -> tuple[int, BaseModel]:
        process = get_service().create_process(req, _caller())
        logger.info("process created", extra={"process_id": process.process_id})
        return 201, process

    return _once("create_process", create)


@app.get("/v1/processes")
@tracer.capture_method
def list_processes() -> Response[str]:
    params = app.current_event.query_string_parameters or {}
    query = _validate(ListProcessesQuery, dict(params), "query")
    return _model(200, get_service().list_processes(query, _caller()))


@app.get("/v1/processes/<process_id>")
@tracer.capture_method
def get_process(process_id: str) -> Response[str]:
    return _model(200, get_service().get_process(process_id, _caller()))


@app.post("/v1/processes/<process_id>/close")
@tracer.capture_method
def close_process(process_id: str) -> Response[str]:
    req = _body(CloseProcessRequest, optional=True)

    def close() -> tuple[int, BaseModel]:
        process = get_service().close_process(process_id, req, _caller())
        logger.info(
            "process closed",
            extra={"process_id": process_id, "expected_count": process.expected_count},
        )
        return 200, process

    return _once(f"close:{process_id}", close)


@app.post("/v1/processes/<process_id>/uploads")
@tracer.capture_method
def create_upload(process_id: str) -> Response[str]:
    req = _body(CreateUploadRequest)

    def issue() -> tuple[int, BaseModel]:
        ticket = get_service().create_upload(process_id, req, _caller())
        logger.info(
            "upload ticket issued",
            extra={
                "process_id": process_id,
                "upload_id": ticket.upload_id,
                "article_id": ticket.article_id,
            },
        )
        return 201, ticket

    return _once(f"upload:{process_id}", issue)


@app.get("/v1/processes/<process_id>/uploads")
@tracer.capture_method
def list_uploads(process_id: str) -> Response[str]:
    params = app.current_event.query_string_parameters or {}
    query = _validate(ListUploadsQuery, dict(params), "query")
    return _model(200, get_service().list_uploads(process_id, query, _caller()))


@app.get("/v1/processes/<process_id>/articles")
@tracer.capture_method
def list_articles(process_id: str) -> Response[str]:
    params = app.current_event.query_string_parameters or {}
    query = _validate(ListArticlesQuery, dict(params), "query")
    return _model(200, get_service().list_articles(process_id, query, _caller()))


@app.get("/v1/uploads/<upload_id>")
@tracer.capture_method
def get_upload(upload_id: str) -> Response[str]:
    return _model(200, get_service().get_upload(upload_id, _caller()))


@app.get("/v1/articles/<article_id>")
@tracer.capture_method
def get_article(article_id: str) -> Response[str]:
    article = get_service().get_article(article_id, _caller())
    return _with_etag(_model(200, article), article.updated_at)


def _with_etag(response: Response[str], version: str) -> Response[str]:
    response.headers["ETag"] = f'"{version}"'
    return response


@app.patch("/v1/articles/<article_id>")
@tracer.capture_method
def patch_article(article_id: str) -> Response[str]:
    req = _body(PatchArticleRequest)
    # If-Match: the ETag from a GET; the PATCH applies only if nobody changed the article since.
    if_match = next(
        (v for k, v in (app.current_event.headers or {}).items() if k.lower() == "if-match"), None
    )
    expected = if_match.strip().strip('"') if if_match and if_match.strip() != "*" else None
    article = get_service().patch_article(article_id, req, _caller(), if_unchanged_since=expected)
    logger.info("article patched", extra={"article_id": article_id})
    return _with_etag(_model(200, article), article.updated_at)


@app.delete("/v1/articles/<article_id>")
@tracer.capture_method
def delete_article(article_id: str) -> Response[str]:
    params = app.current_event.query_string_parameters or {}
    purge = str(params.get("purge", "")).lower() in {"1", "true", "yes"}
    get_service().delete_article(article_id, _caller(), purge=purge)
    logger.info("article deleted", extra={"article_id": article_id})
    return Response(status_code=204, body="")


@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_REST)
@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    _IDEMPOTENCY.register_lambda_context(context)
    return app.resolve(event, context)
