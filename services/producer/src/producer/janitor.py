"""Janitor (hourly, EventBridge Scheduler): settles what nobody else will.

1. Tickets that expired with no outcome. If the object is in the bucket (its S3 event was lost),
   the registrar is asked again with a synthetic ObjectCreated event; otherwise the upload is
   EXPIRED, so its process can settle.
2. OPEN processes nobody has touched for OPEN_PROCESS_IDLE_DAYS are closed, expecting the uploads
   requested so far (a client that died before closing would leave them open forever).

Tunables (config.Settings): TICKET_GRACE_SECONDS, JANITOR_BATCH, OPEN_PROCESS_IDLE_DAYS.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import boto3
from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError

from producer.config import Settings, get_settings
from producer.repository import Item, Repository
from producer.storage import make_s3_client
from producer.timeutil import iso, utcnow

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_sqs import SQSClient

logger = Logger()
metrics = Metrics()

# Beyond the tables and bucket every function needs (config.Settings).
REQUIRED = ("REGISTRAR_QUEUE_URL",)


class Janitor:
    def __init__(self, settings: Settings, repo: Repository, s3: S3Client, sqs: SQSClient) -> None:
        self.settings = settings
        self.repo = repo
        self.s3 = s3
        self.sqs = sqs
        self.queue_url = settings.need("REGISTRAR_QUEUE_URL")
        self.counts: Counter[str] = Counter()

    def run(self, now: datetime) -> dict[str, int]:
        self.counts = Counter()
        self.expired_tickets(now)
        self.idle_processes(now)
        return dict(self.counts)

    def expired_tickets(self, now: datetime) -> None:
        before = int((now - self.settings.ticket_grace).timestamp())
        for upload in self.repo.pending_uploads_before(before, self.settings.janitor_batch):
            if upload.get("status") != "PENDING":
                continue  # REGISTERING: the registrar has it (SQS retries, then its DLQ)
            if self._object_exists(upload):
                self._resend(upload, now)
            elif self.repo.expire_upload(upload, iso(now), self._row_ttl(now)):
                self.counts["expired"] += 1

    def idle_processes(self, now: datetime) -> None:
        cutoff = iso(now - timedelta(days=self.settings.open_process_idle_days))
        for process in self.repo.open_processes_idle_since(cutoff):
            expected = int(process.get("tickets_issued") or 0)
            if self.repo.close_process(str(process["process_id"]), expected, iso(now)):
                self.counts["processes_closed"] += 1
                logger.info(
                    "idle process closed",
                    extra={"process_id": process["process_id"], "expected_count": expected},
                )

    def _object_exists(self, upload: Item) -> bool:
        try:
            self.s3.head_object(Bucket=self.settings.raw_bucket, Key=str(upload["s3_key"]))
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def _resend(self, upload: Item, now: datetime) -> None:
        """The object arrived but its event didn't (or was dropped): ask the registrar again,
        once (the index can lag, so "once" is a conditional write on the upload itself)."""
        if not self.repo.mark_resent(str(upload["upload_id"]), iso(now)):
            return  # asked already, or it has an outcome now
        event = {
            "Records": [
                {
                    "eventSource": "aws:s3",
                    "eventName": "ObjectCreated:Put",
                    "s3": {
                        "bucket": {"name": self.settings.raw_bucket},
                        "object": {"key": upload["s3_key"]},
                    },
                }
            ]
        }
        self.sqs.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps(event))
        self.counts["resent"] += 1

    def _row_ttl(self, now: datetime) -> int:
        return int((now + self.settings.upload_row_ttl).timestamp())


@logger.inject_lambda_context
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, int]:
    settings = get_settings().require(*REQUIRED)
    repo = Repository.from_names(
        settings.process_table, settings.articles_table, settings.uploads_table
    )
    janitor = Janitor(settings, repo, make_s3_client(), boto3.client("sqs"))
    counts = janitor.run(utcnow())
    for name in ("expired", "resent", "processes_closed"):
        metrics.add_metric(name=f"Janitor_{name}", unit=MetricUnit.Count, value=counts.get(name, 0))
    logger.info("janitor ran", extra=counts)
    return counts
