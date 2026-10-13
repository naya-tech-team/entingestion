"""Re-running articles: by id, by process, and failed ones (with the ingest DLQ).

POST /v1/ingest                      re-run articles (MANUAL)
POST /v1/processes/{id}/ingest       re-run a process's articles (via gsi2-process)
POST /v1/dlq/redrive                 re-run failed articles by error_code; replay the DLQ
"""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.event_handler import Response
from aws_lambda_powertools.event_handler.api_gateway import Router
from aws_lambda_powertools.metrics import MetricUnit
from pydantic import Field

from ingest import aws, requeue
from ingest.config import get_settings
from ingest.control_api.http import Request, body, caller, json_response, queue_attributes
from ingest.models.messages import IngestMessage
from ingest.models.status import ArticleStatus, StepName
from ingest.status.store import Item

logger = Logger(child=True)
metrics = Metrics()
router = Router()  # type: ignore[no-untyped-call]

MAX_ARTICLES = 500
FAILED = (ArticleStatus.FAILED_PERMANENT, ArticleStatus.FAILED_RETRYABLE)


class IngestRequest(Request):
    article_ids: list[str] = Field(min_length=1, max_length=MAX_ARTICLES)
    force: bool = False
    from_stage: StepName | None = None
    dry_run: bool = False


class ProcessIngestRequest(Request):
    only_failed: bool = False
    dry_run: bool = False


class RedriveRequest(Request):
    error_code: str | None = None
    max: int = Field(default=100, ge=1, le=1000)
    dry_run: bool = False


def _queue(messages: list[IngestMessage], dry_run: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"dry_run": dry_run, "count": len(messages)}
    if dry_run:
        out["messages"] = [m.model_dump(mode="json", exclude_none=True) for m in messages]
    else:
        out["queued"] = requeue.enqueue(messages)
        metrics.add_metric(name="Requeued_manual", unit=MetricUnit.Count, value=out["queued"])
    return out


def _rerun(article_ids: list[str], **kwargs: Any) -> tuple[list[IngestMessage], list[str]]:
    store = aws.status_store()
    messages, missing = [], []
    by = caller(router.current_event)
    for article_id in dict.fromkeys(article_ids):
        message = requeue.message_for(
            store, article_id, trigger="MANUAL", requested_by=by, **kwargs
        )
        if message is None:
            missing.append(article_id)
        else:
            messages.append(message)
    return messages, missing


@router.post("/v1/ingest")
def ingest() -> Response[str]:
    req = body(router.current_event, IngestRequest)
    messages, missing = _rerun(
        req.article_ids, force=req.force, from_stage=req.from_stage, resume=True
    )
    out = _queue(messages, req.dry_run)
    out["not_found"] = missing
    logger.info("manual ingest", extra={"count": len(messages), "dry_run": req.dry_run})
    return json_response(200 if req.dry_run else 202, out)


@router.post("/v1/processes/<process_id>/ingest")
def ingest_process(process_id: str) -> Response[str]:
    req = body(router.current_event, ProcessIngestRequest)
    latest = aws.status_store().process_articles(process_id)
    if req.only_failed:
        latest = [i for i in latest if i.get("status") in FAILED]
    messages, missing = _rerun([str(i["pk"]) for i in latest], resume=True)
    out = _queue(messages, req.dry_run)
    out |= {"process_id": process_id, "not_found": missing}
    return json_response(200 if req.dry_run else 202, out)


@router.post("/v1/dlq/redrive")
def redrive() -> Response[str]:
    """Re-run articles that failed (optionally only one error_code), resuming where their staged
    output is intact. Without an error_code, messages in the ingest DLQ are moved back too."""
    req = body(router.current_event, RedriveRequest)
    store = aws.status_store()
    failed: list[Item] = []
    for status in FAILED:
        failed += store.by_status(status)
    if req.error_code:
        failed = [i for i in failed if i.get("error_code") == req.error_code]
    messages, missing = _rerun([str(i["pk"]) for i in failed[: req.max]], resume=True)
    out = _queue(messages, req.dry_run)
    out |= {"error_code": req.error_code, "not_found": missing}
    if req.error_code is None:
        out["dlq"] = _redrive_dlq(req.dry_run)
    return json_response(200 if req.dry_run else 202, out)


def _redrive_dlq(dry_run: bool) -> dict[str, Any]:
    queues = get_settings().queues
    dlq = queue_attributes(queues.ingest_dlq_url)
    waiting = int(dlq.get("ApproximateNumberOfMessages", 0))
    if dry_run or waiting == 0:
        return {"messages": waiting, "moved": False}
    task = aws.sqs().start_message_move_task(
        SourceArn=dlq["QueueArn"],
        DestinationArn=queue_attributes(queues.ingest_url)["QueueArn"],
    )
    return {"messages": waiting, "moved": True, "task_handle": task["TaskHandle"]}
