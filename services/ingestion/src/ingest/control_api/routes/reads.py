"""Reads: articles, processes, and search.

GET  /v1/articles?status=            LATEST items in one status, oldest first, paged
GET  /v1/articles/{id}/status        LATEST, last 5 runs, latest run's steps, execution link
GET  /v1/processes/{id}/summary      counters, producer counts, failures by error_code
GET  /v1/processes/{id}/articles     each article's status (slim LATEST), at most 1000
POST /v1/search                      hybrid search of one tenant's chunks (ingest.search);
                                     the caller must read or write a source of the tenant
"""

from __future__ import annotations

from collections import Counter
from datetime import date

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.event_handler import Response
from aws_lambda_powertools.event_handler.api_gateway import Router
from aws_lambda_powertools.metrics import MetricUnit
from entcommon.paging import encode_token
from pydantic import Field, field_validator

from ingest import access, aws, producer
from ingest import search as searching
from ingest.control_api.http import (
    MAX_TENANT_ID,
    Request,
    body,
    caller,
    decode_page_token,
    execution_link,
    json_response,
    limit_param,
    status_param,
)
from ingest.control_api.routes.runs import FAILED

logger = Logger(child=True)
metrics = Metrics()
router = Router()  # type: ignore[no-untyped-call]

MAX_PROCESS_ARTICLES = 1000  # GET /v1/processes/{id}/articles, in article_id order
_ARTICLE_FIELDS = (
    "status", "stage", "doc_version", "chunk_count", "error_code", "error_msg", "updated_at",
    "indexed_at", "tenant_id",
)  # fmt: skip
_LISTED_FIELDS = (
    "status", "stage", "process_id", "tenant_id", "doc_version", "error_code", "error_msg",
    "updated_at",
)  # fmt: skip


class SearchFilters(Request):
    source: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    published_after: date | None = None
    process_id: str | None = Field(default=None, min_length=1, max_length=64)


class SearchBody(Request):
    tenant_id: str = Field(min_length=1, max_length=MAX_TENANT_ID)
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


@router.post("/v1/search")
def search() -> Response[str]:
    """Hybrid search within one tenant. The tenant is checked against the producer's source
    access policy: the caller must read or write one of its sources (or be an admin)."""
    req = body(router.current_event, SearchBody)
    who = caller(router.current_event)
    if not access.policy().can_search(who, req.tenant_id):
        logger.warning("search refused", extra={"tenant_id": req.tenant_id, "by": who})
        metrics.add_metric(name="SearchRefused", unit=MetricUnit.Count, value=1)
        return json_response(403, {"message": f"not allowed to search tenant {req.tenant_id}"})
    out = searching.search(
        tenant_id=req.tenant_id,
        query=req.query,
        k=req.k,
        filters=req.filters.model_dump(mode="json", exclude_none=True),
        rerank=req.rerank,
    )
    metrics.add_metric(name="Search", unit=MetricUnit.Count, value=1)
    return json_response(200, {"tenant_id": req.tenant_id, "query": req.query, "k": req.k, **out})


@router.get("/v1/articles/<article_id>/status")
def article_status(article_id: str) -> Response[str]:
    store = aws.status_store()
    latest = store.latest(article_id)
    if latest is None:
        return json_response(404, {"message": f"no ingestion status for {article_id}"})
    runs = store.recent_runs(article_id, 5)
    steps = [
        i
        for i in store.run_items(article_id, str(latest.get("run_id", "")))
        if "#STEP#" in str(i["sk"])
    ]
    current = next((r for r in runs if r.get("run_id") == latest.get("run_id")), None)
    return json_response(
        200,
        {
            "latest": latest,
            "runs": runs,
            "steps": steps,
            "execution": execution_link(current.get("execution_arn") if current else None),
        },
    )


@router.get("/v1/processes/<process_id>/summary")
def process_summary(process_id: str) -> Response[str]:
    store = aws.status_store()
    summary = store.summary(process_id)
    proc = producer.process(process_id)
    if summary is None and proc is None:
        return json_response(404, {"message": f"unknown process {process_id}"})
    failures = Counter(
        str(i.get("error_code", "unknown"))
        for i in store.process_articles(process_id)
        if i.get("status") in FAILED
    )
    counters = ("indexed", "skipped", "failed")
    return json_response(
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


@router.get("/v1/processes/<process_id>/articles")
def process_articles(process_id: str) -> Response[str]:
    """Each article's ingestion status (LATEST, slim), for the articles last touched by the
    process. An article a later process re-sent counts under that one. An unknown process is
    an empty list: its articles may not have reached ingestion yet."""
    items = sorted(aws.status_store().process_articles(process_id), key=lambda i: str(i["pk"]))
    return json_response(
        200,
        {
            "process_id": process_id,
            "items": [
                {"article_id": i["pk"]} | {k: i.get(k) for k in _ARTICLE_FIELDS}
                for i in items[:MAX_PROCESS_ARTICLES]
            ],
        },
    )


@router.get("/v1/articles")
def articles_by_status() -> Response[str]:
    """One page of the articles in a status (LATEST, slim), oldest first. next_token continues;
    it is null on the last page (or after a full page that happens to be the last)."""
    event = router.current_event
    status = status_param(event)
    limit = limit_param(event)
    start = decode_page_token(event.get_query_string_value("next_token"), status)
    items, last_key = aws.status_store().page_by_status(status, start, limit)
    return json_response(
        200,
        {
            "items": [
                {"article_id": i["pk"]} | {k: i.get(k) for k in _LISTED_FIELDS} for i in items
            ],
            "next_token": encode_token(last_key),
        },
    )
