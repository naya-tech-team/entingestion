"""MarkFailed: the state machine's Catch target, the last write of a run that failed.

- IngestSkip (e.g. no text)                -> SKIPPED, terminal, counted as skipped
- throttling after retries                 -> FAILED_RETRYABLE on its own budget (throttle_count,
                                              RETRY_MAX_THROTTLED): a busy account doesn't use up
                                              the retries meant for real failures
- other transient after retries (timeouts) -> FAILED_RETRYABLE with next_retry_at; the reconciler
                                              re-queues, and after RETRY_MAX_RETRYABLE tries it's
                                              FAILED_PERMANENT
- everything else                          -> FAILED_PERMANENT, terminal, replayable

Permanent failures are counted (ArticlesFailedPermanent by error_code, which the alarms watch)
and reported in the reconciler's hourly digest, not one notification per article.
"""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext

from ingest import aws, telemetry
from ingest.config import get_settings, require
from ingest.errors import DeletePending, ErrorCode, IngestStale, parse_cause
from ingest.models.status import ArticleStatus, RunOutcome
from ingest.status import keys
from ingest.status.store import StepRef
from ingest.steps.handover import hand_over_delete
from ingest.steps.rollup import complete_process
from ingest.timeutil import now_epoch

logger = Logger()
tracer = Tracer()
metrics = Metrics()

# Throttled after the state machine's (long) retries: Lambda concurrency or Bedrock quota.
THROTTLED = frozenset({"IngestThrottled", "Lambda.TooManyRequestsException"})
TRANSIENT = frozenset(
    {
        "IngestTransient",
        "Lambda.ServiceException",
        "Lambda.AWSLambdaException",
        "Lambda.SdkClientException",
        "States.Timeout",
    }
)
REQUIRED = ("status_table", "producer_process_table")
_FINISHED = frozenset({RunOutcome.INDEXED.value, RunOutcome.DELETED.value})
# Errors raised by Step Functions or Lambda itself carry no IngestError JSON in their cause.
_CODE_BY_NAME = {
    "States.Timeout": ErrorCode.TIMEOUT.value,
    "Lambda.TooManyRequestsException": ErrorCode.THROTTLED.value,
    "Lambda.ServiceException": ErrorCode.UPSTREAM_5XX.value,
    "Lambda.AWSLambdaException": ErrorCode.UPSTREAM_5XX.value,
    "Lambda.SdkClientException": ErrorCode.UPSTREAM_5XX.value,
}


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    require(*REQUIRED)
    ctx = event["ctx"]
    error = event.get("error") or {}
    name = str(error.get("Error", "States.TaskFailed"))
    parsed = parse_cause(error.get("Cause"))
    code = parsed["code"] if name.startswith("Ingest") else _CODE_BY_NAME.get(name, parsed["code"])
    detail = parsed["detail"]
    message = ctx["message"]
    # If Claim itself failed, ctx is still the dispatcher's input (no article_id yet).
    article_id = ctx.get("article_id") or message["article_id"]
    lane = ctx.get("lane")
    ref = StepRef(
        article_id,
        ctx["run_id"],
        message.get("process_id"),
        lane,
        int(ctx["doc_version"]) if lane and ctx.get("doc_version") else None,
        ctx.get("slot"),
    )
    logger.append_keys(article_id=ref.article_id, run_id=ref.run_id, process_id=ref.process_id)

    store = aws.status_store()
    run = store.get(keys.run_key(ref.article_id, ref.run_id)) or {}
    if run.get("outcome") in _FINISHED:
        # The run already ended well (e.g. Finalize failed after finishing): don't undo it.
        logger.info("run already finished", extra={"outcome": run["outcome"]})
        store.release_slot(ref.slot, ref.run_id)
        return {"outcome": run["outcome"]}
    store.fail_running_step(ref, code, detail)  # timeouts/crashes the step couldn't record
    try:
        outcome = _settle(store, ref, name, code, detail)
    except DeletePending:
        hand_over_delete(store, ref)
        store.release_slot(ref.slot, ref.run_id)
        return {"outcome": RunOutcome.STALE.value}
    except IngestStale:
        logger.info("run no longer owns the article; nothing to mark")
        store.release_slot(ref.slot, ref.run_id)
        return {"outcome": RunOutcome.STALE.value}

    metrics.add_metric(name=f"Run_{outcome}", unit=MetricUnit.Count, value=1)
    if lane is not None:  # a shadow run: the drift check re-queues what's still missing
        logger.warning("shadow run ended", extra={"outcome": outcome, "table": lane})
        return {"outcome": outcome, "error_code": code}
    if outcome == RunOutcome.FAILED_PERMANENT.value:
        telemetry.emit("ArticlesFailedPermanent", 1, error_code=code)
    if outcome != RunOutcome.FAILED_RETRYABLE.value:
        complete_process(store, ref.process_id)
    logger.warning("run ended", extra={"outcome": outcome, "error_code": code, "error_type": name})
    return {"outcome": outcome, "error_code": code}


def _settle(store: Any, ref: StepRef, name: str, code: str, detail: str) -> str:
    error_sets = {"error_class": "permanent", "error_code": code, "error_msg": detail}
    budget = get_settings().retry
    if name == "IngestSkip":
        store.finish(ref, ArticleStatus.SKIPPED, RunOutcome.SKIPPED, {"skip_reason": code})
        return RunOutcome.SKIPPED.value
    if name in THROTTLED:
        latest = store.latest(ref.article_id) or {}
        throttled = int(latest.get("throttle_count", 0))
        if throttled + 1 < budget.max_throttled:
            backoff = budget.throttle_seconds * 2 ** min(throttled, 3)  # 5, 10, 20, 40 min
            store.mark_retryable(ref, code, detail, now_epoch() + backoff, counter="throttle_count")
            return RunOutcome.FAILED_RETRYABLE.value
        detail = f"gave up after {budget.max_throttled} throttled runs: {detail}"
        error_sets |= {"error_msg": detail[:1024]}
    elif name in TRANSIENT:
        latest = store.latest(ref.article_id) or {}
        retries = int(latest.get("retry_count", 0))
        if retries + 1 < budget.max_retryable:
            next_at = now_epoch() + budget.base_seconds * 2**retries
            store.mark_retryable(ref, code, detail, next_at)
            return RunOutcome.FAILED_RETRYABLE.value
        detail = f"gave up after {budget.max_retryable} retryable failures: {detail}"
        error_sets |= {"error_msg": detail[:1024]}
    store.finish(ref, ArticleStatus.FAILED_PERMANENT, RunOutcome.FAILED_PERMANENT, error_sets)
    return RunOutcome.FAILED_PERMANENT.value
