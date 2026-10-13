"""SQS (S3 ObjectCreated) -> registrar Lambda handler."""

from __future__ import annotations

import json
from functools import lru_cache
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

from producer.config import get_settings
from producer.registration import Registrar, object_refs
from producer.repository import Repository
from producer.storage import make_s3_client

logger = Logger()
tracer = Tracer()
metrics = Metrics()
processor = BatchProcessor(event_type=EventType.SQS)


@lru_cache(maxsize=1)
def get_registrar() -> Registrar:
    settings = get_settings()
    repo = Repository.from_names(
        settings.process_table, settings.articles_table, settings.uploads_table
    )
    return Registrar(settings, repo, make_s3_client())


@tracer.capture_method
def record_handler(record: SQSRecord) -> None:
    """Business outcomes return normally; unexpected errors raise so SQS retries (then DLQ)."""
    event = json.loads(record.body)
    for ref in object_refs(event):
        try:
            result = get_registrar().register(ref)
        except Exception:
            # The batch processor reports the record for retry but logs nothing itself.
            logger.exception("object failed; SQS will retry", extra={"s3_key": ref.key})
            raise
        fields = {
            "outcome": result.outcome.value,
            "upload_id": result.upload_id,
            "article_id": result.article_id,
            "process_id": result.process_id,
            "reason": result.reason,
            "s3_key": ref.key,
        }
        logger.info("object processed", extra={k: v for k, v in fields.items() if v is not None})
        metrics.add_metric(name=f"Upload_{result.outcome.value}", unit=MetricUnit.Count, value=1)
        if result.reason == "unknown_upload":
            # A well-formed key with no upload row: lost tickets or a very late redrive. Alarmed.
            metrics.add_metric(name="UnknownUpload", unit=MetricUnit.Count, value=1)


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics
def lambda_handler(event: dict[str, Any], context: LambdaContext) -> PartialItemFailureResponse:
    return process_partial_response(
        event=event, record_handler=record_handler, processor=processor, context=context
    )
