"""Control API handler (brief §10). API Gateway authorizes every call with IAM; the invoke
policies split readers (GET), searchers (POST /v1/search) and operators (everything).

    POST /v1/ingest                      re-run articles (MANUAL)
    POST /v1/processes/{id}/ingest       re-run a process's articles (via gsi2-process)
    POST /v1/dlq/redrive                 re-run failed articles by error_code; replay the DLQ
    GET  /v1/articles/{id}/status        LATEST, last 5 runs, latest run's steps, execution link
    GET  /v1/processes/{id}/summary      counters, producer counts, failures by error_code
    GET  /v1/processes/{id}/articles     each article's status (slim LATEST), at most 1000
    POST /v1/reindex                     reindex/rebuild into a vector table (state machine)
    GET  /v1/tables                      live table, write tables, row counts per table
    POST /v1/tables/active               switch the live table (go live / roll back), gated
    DELETE /v1/tables/writes/{table}     stop writing a table (retire it, or cancel a change)

The table routes and reindex take an optional tenant_id: a tenant with its own vector-table
registry (ingest.vectorstore.registry) moves to another model on its own. Without one they act
on the default registry, whose live table chunks_active also serves.
    POST /v1/search                      hybrid search of one tenant's chunks (ingest.search);
                                         the caller must read or write a source of the tenant
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from typing import Any

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response, content_types
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ingest import access, aws, evaluation, producer, requeue
from ingest import search as searching
from ingest.config import get_settings
from ingest.ids import new_run_id
from ingest.models.messages import IngestMessage
from ingest.models.status import ArticleStatus, StepName
from ingest.reindex import ReindexFilter, ReindexRequest, plan_items
from ingest.status.store import Item, StatusStore
from ingest.tenancy import Tenancy
from ingest.vectorstore import registry
from ingest.vectorstore.targets import TARGETS

logger = Logger()
tracer = Tracer()
metrics = Metrics()
app = APIGatewayRestResolver()

MAX_ARTICLES = 500
MAX_PROCESS_ARTICLES = 1000  # GET /v1/processes/{id}/articles, in article_id order
_ARTICLE_FIELDS = (
    "status", "stage", "doc_version", "chunk_count", "error_code", "error_msg", "updated_at",
    "indexed_at", "tenant_id",
)  # fmt: skip
_FAILED = (ArticleStatus.FAILED_PERMANENT, ArticleStatus.FAILED_RETRYABLE)


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IngestRequest(_Request):
    article_ids: list[str] = Field(min_length=1, max_length=MAX_ARTICLES)
    force: bool = False
    from_stage: StepName | None = None
    dry_run: bool = False


class ProcessIngestRequest(_Request):
    only_failed: bool = False
    dry_run: bool = False


class RedriveRequest(_Request):
    error_code: str | None = None
    max: int = Field(default=100, ge=1, le=1000)
    dry_run: bool = False


class ReindexBody(_Request):
    filter: ReindexFilter = Field(default_factory=ReindexFilter)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=128)
    target_table: str | None = None
    from_stage: StepName = StepName.EMBED
    dry_run: bool = False


class ActivateBody(_Request):
    table: str
    tenant_id: str | None = Field(default=None, min_length=1, max_length=128)
    force: bool = False  # switch despite missing articles or a weaker (or no) evaluation


class SearchFilters(_Request):
    source: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    published_after: date | None = None
    process_id: str | None = Field(default=None, min_length=1, max_length=64)


class SearchBody(_Request):
    tenant_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=10, ge=1, le=50)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    rerank: bool = False

    @field_validator("query")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query is blank")
        return value


class BadRequest(Exception):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("request validation failed")
        self.errors = errors


# -- helpers ----------------------------------------------------------------


def _json(status: int, body: Any) -> Response[str]:
    return Response(
        status_code=status,
        content_type=content_types.APPLICATION_JSON,
        body=json.dumps(body, separators=(",", ":"), default=str),
    )


def _body[ModelT: BaseModel](model: type[ModelT]) -> ModelT:
    raw = app.current_event.decoded_body or ""
    try:
        data = json.loads(raw) if raw.strip() else {}
        return model.model_validate(data)
    except json.JSONDecodeError as err:
        raise BadRequest([{"loc": ["body"], "msg": f"invalid JSON: {err.msg}"}]) from err
    except ValidationError as err:
        raise BadRequest(
            [{"loc": list(e["loc"]), "msg": e["msg"]} for e in err.errors(include_url=False)]
        ) from err


def _caller() -> str:
    identity = app.current_event.request_context.identity
    return identity.user_arn or identity.caller or "unknown"


def _queue(messages: list[IngestMessage], dry_run: bool) -> dict[str, Any]:
    body: dict[str, Any] = {"dry_run": dry_run, "count": len(messages)}
    if dry_run:
        body["messages"] = [m.model_dump(mode="json", exclude_none=True) for m in messages]
    else:
        body["queued"] = requeue.enqueue(messages)
        metrics.add_metric(name="Requeued_manual", unit=MetricUnit.Count, value=body["queued"])
    return body


def _rerun(article_ids: list[str], **kwargs: Any) -> tuple[list[IngestMessage], list[str]]:
    store = aws.status_store()
    messages, missing = [], []
    for article_id in dict.fromkeys(article_ids):
        message = requeue.message_for(
            store, article_id, trigger="MANUAL", requested_by=_caller(), **kwargs
        )
        if message is None:
            missing.append(article_id)
        else:
            messages.append(message)
    return messages, missing


def _execution_link(arn: str | None) -> str | None:
    parts = (arn or "").split(":")
    if len(parts) < 7 or parts[0] != "arn":  # noqa: PLR2004 - arn:partition:states:region:...
        return None
    region = parts[3]
    return (
        f"https://{region}.console.aws.amazon.com/states/home?region={region}"
        f"#/v2/executions/details/{arn}"
    )


# -- writes -----------------------------------------------------------------


@app.post("/v1/ingest")
def ingest() -> Response[str]:
    req = _body(IngestRequest)
    messages, missing = _rerun(
        req.article_ids, force=req.force, from_stage=req.from_stage, resume=True
    )
    body = _queue(messages, req.dry_run)
    body["not_found"] = missing
    logger.info("manual ingest", extra={"count": len(messages), "dry_run": req.dry_run})
    return _json(200 if req.dry_run else 202, body)


@app.post("/v1/processes/<process_id>/ingest")
def ingest_process(process_id: str) -> Response[str]:
    req = _body(ProcessIngestRequest)
    latest = aws.status_store().process_articles(process_id)
    if req.only_failed:
        latest = [i for i in latest if i.get("status") in _FAILED]
    messages, missing = _rerun([str(i["pk"]) for i in latest], resume=True)
    body = _queue(messages, req.dry_run)
    body |= {"process_id": process_id, "not_found": missing}
    return _json(200 if req.dry_run else 202, body)


@app.post("/v1/dlq/redrive")
def redrive() -> Response[str]:
    """Re-run articles that failed (optionally only one error_code), resuming where their staged
    output is intact. Without an error_code, messages in the ingest DLQ are moved back too."""
    req = _body(RedriveRequest)
    store = aws.status_store()
    failed: list[Item] = []
    for status in _FAILED:
        failed += store.by_status(status)
    if req.error_code:
        failed = [i for i in failed if i.get("error_code") == req.error_code]
    messages, missing = _rerun([str(i["pk"]) for i in failed[: req.max]], resume=True)
    body = _queue(messages, req.dry_run)
    body |= {"error_code": req.error_code, "not_found": missing}
    if req.error_code is None:
        body["dlq"] = _redrive_dlq(req.dry_run)
    return _json(200 if req.dry_run else 202, body)


def _redrive_dlq(dry_run: bool) -> dict[str, Any]:
    settings = get_settings()
    dlq = _queue_attributes(settings.ingest_dlq_url)
    waiting = int(dlq.get("ApproximateNumberOfMessages", 0))
    if dry_run or waiting == 0:
        return {"messages": waiting, "moved": False}
    task = aws.sqs().start_message_move_task(
        SourceArn=dlq["QueueArn"],
        DestinationArn=_queue_attributes(settings.ingest_queue_url)["QueueArn"],
    )
    return {"messages": waiting, "moved": True, "task_handle": task["TaskHandle"]}


def _queue_attributes(url: str) -> dict[str, str]:
    resp = aws.sqs().get_queue_attributes(
        QueueUrl=url, AttributeNames=["QueueArn", "ApproximateNumberOfMessages"]
    )
    return {str(k): v for k, v in resp["Attributes"].items()}


@app.post("/v1/reindex")
def reindex() -> Response[str]:
    """Re-run indexed articles into a vector table (brief §7.3): from_stage EMBED re-embeds into
    a new table (model change), INDEX reloads staged vectors (rebuild, no Bedrock calls)."""
    req = _body(ReindexBody)
    store = aws.status_store()
    tenant = registry.scope(req.tenant_id)
    request = ReindexRequest(
        filter=req.filter,
        target_table=req.target_table or registry.load(store, tenant).live,
        from_stage=req.from_stage,
        requested_by=_caller(),
        tenant_id=tenant,
    )
    problems = request.check()
    if problems:
        raise BadRequest([{"loc": ["body"], "msg": p} for p in problems])
    count = len(plan_items(store, request.filter, request.target_table, tenant))
    body: dict[str, Any] = {
        "dry_run": req.dry_run,
        "count": count,
        "tenant_id": tenant,
        "target_table": request.target_table,
        "from_stage": request.from_stage.value,
    }
    if not req.dry_run:
        # The tenant's live runs write the table from now on, so edits made while it fills
        # aren't missed (a tenant's first change gives it its own registry).
        body["tables"] = registry.add_write(
            store, request.target_table, _caller(), tenant
        ).as_dict()
        name = f"reindex-{request.target_table}-{new_run_id()}"[:80]
        resp = aws.sfn().start_execution(
            stateMachineArn=get_settings().reindex_state_machine_arn,
            name=name,
            input=request.model_dump_json(),
        )
        body["execution"] = _execution_link(resp["executionArn"])
        body["execution_arn"] = resp["executionArn"]
        logger.info("reindex started", extra=body)
    return _json(200 if req.dry_run else 202, body)


def _tenant_param() -> str | None:
    value = app.current_event.get_query_string_value("tenant_id") or None
    if value is not None and len(value) > 128:  # noqa: PLR2004 - as IngestMessage.tenant_id
        raise BadRequest([{"loc": ["query", "tenant_id"], "msg": "at most 128 characters"}])
    return registry.scope(value)


@app.get("/v1/tables")
def tables() -> Response[str]:
    """Which vector table chunks_active serves, the registry (live table and write tables) of
    the tenant (?tenant_id=; default: the default registry), row counts, every tenant's own
    registry, and the embedding profiles."""
    tenant = _tenant_param()
    out = _tables({"action": "status"})
    store = aws.status_store()
    current = registry.load(store, tenant)
    out["tenant_id"] = tenant
    out["registry"] = current.as_dict()
    # chunks_active serves the default registry's live table; a tenant reads its table directly.
    out["consistent"] = (
        out.get("active_table") == current.live
        if current.tenant is None
        else (out.get("tables") or {}).get(current.live) is not None
    )
    out["tenants"] = {t: r.as_dict() for t, r in registry.load_all(store).items() if t is not None}
    out["profiles"] = {
        name: {
            "table": p.table,
            "provider": p.provider,
            "model": p.model,
            "dimension": p.dimension,
        }
        for name, p in get_settings().embedding_profiles.items()
    }
    return _json(200, out)


@app.post("/v1/tables/active")
def activate_table() -> Response[str]:
    """Switch chunks_active to another table (go live after a reindex, or roll back).

    Refused (409) unless live runs write the table, every indexed article's current version is
    in it, and its newest evaluation (at most EVAL_MAX_AGE_DAYS old) has recall@10 no more than
    EVAL_TOLERANCE below the live table's on the same golden set; force overrides the coverage
    and evaluation checks (a rollback to an old table usually needs it)."""
    req = _body(ActivateBody)
    if req.table not in TARGETS:
        raise BadRequest([{"loc": ["body", "table"], "msg": f"known tables: {sorted(TARGETS)}"}])
    store = aws.status_store()
    tenant = registry.scope(req.tenant_id)
    current = registry.load(store, tenant)
    if req.table not in current.writes:
        return _json(
            409,
            {
                "message": f"{req.table} isn't written by live runs; reindex into it first",
                "tables": current.as_dict(),
            },
        )
    missing = coverage_gaps(store, req.table, tenant)
    if missing and not req.force:
        return _json(
            409,
            {
                "message": f"{len(missing)} indexed articles aren't in {req.table} yet",
                "missing": len(missing),
                "sample": missing[:20],
                "hint": "reindex with filter.only_uncovered, or wait for the drift check",
            },
        )
    if not req.force:
        settings = get_settings()
        refused = evaluation.switch_gate(
            req.table,
            current.live,
            tolerance=settings.eval_tolerance,
            max_age_days=settings.eval_max_age_days,
        )
        if refused is not None:
            return _json(409, refused | {"gate": "evaluation"})
    if current.tenant is None and tenant is None:
        out = _tables({"action": "activate", "table": req.table})  # chunks_active follows
    else:  # a tenant's own registry: its searches read the table directly
        out = {"previous": current.live, "active_table": req.table}
    out["tenant_id"] = tenant
    out["tables"] = registry.set_live(store, req.table, _caller(), tenant).as_dict()
    logger.warning("active table switched", extra=out | {"by": _caller(), "forced": req.force})
    return _json(200, out)


@app.delete("/v1/tables/writes/<table>")
def retire_table(table: str) -> Response[str]:
    """Stop writing a table: retire the old table after a switch, or cancel a model change.
    ?tenant_id= for a tenant's own registry. The table's rows stay (a later rollback)."""
    tenant = _tenant_param()
    try:
        out = registry.remove_write(aws.status_store(), table, _caller(), tenant)
    except registry.RegistryError as err:
        return _json(409, {"message": str(err)})
    logger.warning("write table retired", extra={"table": table, "tenant": tenant})
    return _json(200, out.as_dict())


def coverage_gaps(store: StatusStore, table: str, tenant: str | None = None) -> list[str]:
    """Indexed articles of `tenant` (None: of the default registry) whose current version isn't
    in `table` yet."""
    attr = registry.version_attr(table)
    indexed = store.by_status(ArticleStatus.INDEXED)
    gaps = [i for i in indexed if i.get(attr) != i.get("doc_version")]
    return [str(i["pk"]) for i in Tenancy(store).of_tenant(gaps, tenant)]


def _tables(payload: dict[str, Any]) -> dict[str, Any]:
    """Vector-table operations run in the tables function (inside the VPC, as ingest_ops)."""
    resp = aws.lambda_client().invoke(
        FunctionName=get_settings().tables_function_name, Payload=json.dumps(payload).encode()
    )
    out = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        raise RuntimeError(f"tables {payload['action']} failed: {out.get('errorMessage', out)}")
    return dict(out)


# -- search -----------------------------------------------------------------


@app.post("/v1/search")
def search() -> Response[str]:
    """Hybrid search within one tenant. The tenant is checked against the producer's source
    access policy: the caller must read or write one of its sources (or be an admin)."""
    req = _body(SearchBody)
    caller = _caller()
    if not access.policy().can_search(caller, req.tenant_id):
        logger.warning("search refused", extra={"tenant_id": req.tenant_id, "by": caller})
        metrics.add_metric(name="SearchRefused", unit=MetricUnit.Count, value=1)
        return _json(403, {"message": f"not allowed to search tenant {req.tenant_id}"})
    out = searching.search(
        tenant_id=req.tenant_id,
        query=req.query,
        k=req.k,
        filters=req.filters.model_dump(mode="json", exclude_none=True),
        rerank=req.rerank,
    )
    metrics.add_metric(name="Search", unit=MetricUnit.Count, value=1)
    return _json(200, {"tenant_id": req.tenant_id, "query": req.query, "k": req.k, **out})


# -- reads ------------------------------------------------------------------


@app.get("/v1/articles/<article_id>/status")
def article_status(article_id: str) -> Response[str]:
    store = aws.status_store()
    latest = store.latest(article_id)
    if latest is None:
        return _json(404, {"message": f"no ingestion status for {article_id}"})
    runs = store.recent_runs(article_id, 5)
    steps = [
        i
        for i in store.run_items(article_id, str(latest.get("run_id", "")))
        if "#STEP#" in str(i["sk"])
    ]
    current = next((r for r in runs if r.get("run_id") == latest.get("run_id")), None)
    return _json(
        200,
        {
            "latest": latest,
            "runs": runs,
            "steps": steps,
            "execution": _execution_link(current.get("execution_arn") if current else None),
        },
    )


@app.get("/v1/processes/<process_id>/summary")
def process_summary(process_id: str) -> Response[str]:
    store = aws.status_store()
    summary = store.summary(process_id)
    proc = producer.process(process_id)
    if summary is None and proc is None:
        return _json(404, {"message": f"unknown process {process_id}"})
    failures = Counter(
        str(i.get("error_code", "unknown"))
        for i in store.process_articles(process_id)
        if i.get("status") in _FAILED
    )
    counters = ("indexed", "skipped", "failed")
    return _json(
        200,
        {
            "process_id": process_id,
            "status": (summary or {}).get("status", "IN_PROGRESS"),
            "counts": {k: int((summary or {}).get(k, 0)) for k in counters},
            "producer": {
                k: (proc or {}).get(k)
                for k in ("status", "expected_count", "registered_count", "rejected_count")
            },
            "failures_by_error_code": dict(failures),
        },
    )


@app.get("/v1/processes/<process_id>/articles")
def process_articles(process_id: str) -> Response[str]:
    """Each article's ingestion status (LATEST, slim), for the articles last touched by the
    process. An article a later process re-sent counts under that one. An unknown process is
    an empty list: its articles may not have reached ingestion yet."""
    items = sorted(aws.status_store().process_articles(process_id), key=lambda i: str(i["pk"]))
    return _json(
        200,
        {
            "process_id": process_id,
            "items": [
                {"article_id": i["pk"]} | {k: i.get(k) for k in _ARTICLE_FIELDS}
                for i in items[:MAX_PROCESS_ARTICLES]
            ],
        },
    )


@app.exception_handler(BadRequest)  # type: ignore[untyped-decorator]
def _on_bad_request(err: BadRequest) -> Response[str]:
    return _json(422, {"message": "request validation failed", "errors": err.errors})


@app.not_found
def _on_not_found(_: Exception) -> Response[str]:
    return _json(404, {"message": "route not found"})


@app.exception_handler(Exception)  # type: ignore[untyped-decorator]
def _on_unexpected(err: Exception) -> Response[str]:
    logger.exception("unhandled error")
    return _json(500, {"message": "internal error"})


@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_REST)
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)
