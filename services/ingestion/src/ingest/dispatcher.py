"""Dispatcher: SQS ingest/backfill queues -> one Step Functions execution per article
version per process.

Bodies are either an IngestMessage (reconciler, control API) or a raw DynamoDB stream record that
the Pipe forwarded. The execution name is the dedupe key (ids.execution_name), so a record
delivered 3x starts one execution; ExecutionAlreadyExists is acknowledged and dropped.

Admission control: a run starts only with a slot of its lane (live: the ingest queue, backfill:
the backfill queue), #SLOTS/<lane>#<i> in ingestion_status, held until the run ends. When every
slot is busy the invocation waits up to SLOT_WAIT_SECONDS for one, then hands the rest of its
batch back to the queue (batch item failures, visible again after
DISPATCH_BUSY_VISIBILITY_SECONDS): work waits on the
queue, where its age is the real backlog, not as running executions. A body that can never
start (unparseable) goes straight to the lane's DLQ, so the queues' high maxReceiveCount only
covers waiting.
"""

from __future__ import annotations

import json
import time
from typing import Any

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.batch import (
    BatchProcessor,
    EventType,
    process_partial_response,
)
from aws_lambda_powertools.utilities.batch.types import PartialItemFailureResponse
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
from aws_lambda_powertools.utilities.typing import LambdaContext
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from ingest import aws, producer
from ingest.config import get_settings, require
from ingest.ids import execution_name, new_run_id
from ingest.models.messages import IngestMessage

logger = Logger()
tracer = Tracer()
metrics = Metrics()
# A whole batch waiting for slots is normal under load, not a function error: report the records
# as failures and return (Dispatch_error counts real start failures, and has its own alarm).
processor = BatchProcessor(event_type=EventType.SQS, raise_on_entire_batch_failure=False)
_deserializer = TypeDeserializer()

REQUIRED = ("status_table", "state_machine_arn")
LIVE = "live"
BACKFILL = "backfill"
# Leave this much of the invocation for the records after the wait and the response.
_RESERVE_MS = 15_000


class SlotsBusy(Exception):
    """Every slot of the lane is held; the record goes back to its queue."""


class _Invocation:
    """Per invocation: when waiting for a slot must stop."""

    deadline = 0.0


def _image(raw: dict[str, Any] | None) -> dict[str, Any]:
    return {k: _deserializer.deserialize(v) for k, v in (raw or {}).items()}


def from_stream_record(record: dict[str, Any]) -> IngestMessage:
    """Map a producer `articles` stream record (Pipe output) to an IngestMessage."""
    ddb = record["dynamodb"]
    if record["eventName"] == "REMOVE":
        old = _image(ddb.get("OldImage")) or _image(ddb.get("Keys"))
        return IngestMessage(
            article_id=str(old["article_id"]),
            event="REMOVE",
            source_updated_at=old.get("updated_at"),
            trigger="STREAM",
        )
    new = _image(ddb.get("NewImage"))
    return IngestMessage(
        article_id=str(new["article_id"]),
        process_id=new.get("process_id"),
        event="UPSERT",
        s3_uri=new.get("s3_uri"),
        s3_version_id=new.get("s3_version_id"),
        content_sha256=new.get("content_sha256"),
        source_updated_at=new.get("updated_at"),
        tenant_id=new.get("tenant_id") or None,
        trigger="STREAM",
        **producer.fingerprints(new),
    )


def to_message(body: dict[str, Any]) -> IngestMessage:
    if "eventName" in body and "dynamodb" in body:
        return from_stream_record(body)
    return IngestMessage.model_validate(body)


def admit(lane: str, run_id: str) -> str | None:
    """A slot of the lane for this run, waiting for one until the invocation's deadline. None
    when the lane has no limit (size 0). Raises SlotsBusy."""
    settings = get_settings()
    size = settings.admission.backfill_slots if lane == BACKFILL else settings.admission.live_slots
    if size <= 0:
        return None
    store = aws.status_store()
    while True:
        slot = store.acquire_slot(lane, size, run_id)
        if slot is not None:
            return slot
        poll = settings.admission.slot_poll_seconds
        if time.monotonic() + poll > _Invocation.deadline:
            raise SlotsBusy(f"all {size} {lane} slots are busy")
        time.sleep(poll)


def start(message: IngestMessage, lane: str = LIVE) -> str:
    """Start the execution for a message. Returns 'started' or 'duplicate'; raises SlotsBusy."""
    settings = get_settings()
    run_id = new_run_id()
    name = execution_name(
        message.article_id,
        content_sha256=message.content_sha256,
        process_id=message.process_id,
        source_updated_at=message.source_updated_at,
        pipeline_version=settings.pipeline_version,
        # Only stream deliveries dedupe by name (DUP). Replays, operator and reconciler requests
        # are always new runs; a fixed name would collide with the execution they're redoing.
        replay_run_id=run_id if message.is_replay or message.trigger != "STREAM" else None,
    )
    slot = admit(lane, run_id)
    ctx: dict[str, Any] = {"message": message.model_dump(mode="json"), "run_id": run_id}
    if slot is not None:
        ctx["slot"] = slot
    try:
        aws.sfn().start_execution(
            stateMachineArn=settings.state_machine_arn, name=name, input=json.dumps({"ctx": ctx})
        )
    except ClientError as err:
        aws.status_store().release_slot(slot, run_id)
        if err.response.get("Error", {}).get("Code") == "ExecutionAlreadyExists":
            logger.info("duplicate delivery dropped", extra={"execution_name": name})
            return "duplicate"
        raise
    except Exception:
        aws.status_store().release_slot(slot, run_id)
        raise
    logger.info("execution started", extra={"execution_name": name, "run_id": run_id})
    return "started"


def lane_of(record: SQSRecord) -> str:
    backfill = get_settings().queues.backfill_arn
    return BACKFILL if backfill and record.event_source_arn == backfill else LIVE


def _queue_url(queue_arn: str) -> str:
    _, partition, _, region, account, name = queue_arn.split(":", 5)
    domain = "amazonaws.com.cn" if partition == "aws-cn" else "amazonaws.com"
    return f"https://sqs.{region}.{domain}/{account}/{name}"


def _wait_on_queue(record: SQSRecord) -> None:
    """Make a record that found no slot visible again soon. Best effort: otherwise it waits
    out the queue's visibility timeout."""
    try:
        aws.sqs().change_message_visibility(
            QueueUrl=_queue_url(record.event_source_arn),
            ReceiptHandle=record.receipt_handle,
            # Visible again soon, not after the queue's visibility timeout.
            VisibilityTimeout=get_settings().admission.busy_visibility_seconds,
        )
    except Exception:
        logger.warning("couldn't shorten the visibility of a waiting record", exc_info=True)


def _dead_letter(record: SQSRecord, lane: str, reason: str) -> None:
    """A body that can never start: straight to the lane's DLQ (the DLQ alarm fires), instead
    of waiting out maxReceiveCount."""
    settings = get_settings()
    url = settings.queues.backfill_dlq_url if lane == BACKFILL else settings.queues.ingest_dlq_url
    if not url:
        raise ValueError(reason)  # no DLQ configured: let the queue's redrive policy move it
    aws.sqs().send_message(
        QueueUrl=url,
        MessageBody=record.body,
        MessageAttributes={"reason": {"DataType": "String", "StringValue": reason[:1024]}},
    )


@tracer.capture_method
def record_handler(record: SQSRecord) -> None:
    lane = lane_of(record)
    try:
        message = to_message(json.loads(record.body))
    except (ValueError, KeyError, TypeError) as exc:  # pydantic ValidationError is a ValueError
        logger.error("unusable message sent to the DLQ", extra={"lane": lane, "error": str(exc)})
        _dead_letter(record, lane, f"{type(exc).__name__}: {exc}")
        metrics.add_metric(name="Dispatch_poison", unit=MetricUnit.Count, value=1)
        return
    logger.append_keys(
        article_id=message.article_id, process_id=message.process_id, trigger=message.trigger
    )
    try:
        outcome = start(message, lane)
    except SlotsBusy:
        metrics.add_metric(name=f"Dispatch_waiting_{lane}", unit=MetricUnit.Count, value=1)
        _wait_on_queue(record)
        raise
    except Exception:
        metrics.add_metric(name="Dispatch_error", unit=MetricUnit.Count, value=1)
        raise
    metrics.add_metric(name=f"Dispatch_{outcome}", unit=MetricUnit.Count, value=1)


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> PartialItemFailureResponse:
    require(*REQUIRED)
    budget_ms = context.get_remaining_time_in_millis() - _RESERVE_MS
    wait_s = min(float(get_settings().admission.slot_wait_seconds), max(0.0, budget_ms / 1000))
    _Invocation.deadline = time.monotonic() + wait_s
    return process_partial_response(
        event=event, record_handler=record_handler, processor=processor, context=context
    )
