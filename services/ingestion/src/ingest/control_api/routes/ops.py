"""Operations overview.

GET  /v1/ops/overview                in-progress/failed counts and oldest; queue depths
"""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import Response
from aws_lambda_powertools.event_handler.api_gateway import Router
from botocore.exceptions import BotoCoreError, ClientError
from entcommon.timeutil import iso_now

from ingest import aws
from ingest.config import get_settings
from ingest.control_api.http import json_response, queue_attributes
from ingest.models.status import TERMINAL_STATUSES, ArticleStatus

logger = Logger(child=True)
router = Router()  # type: ignore[no-untyped-call]

# Every status an article can be stuck in, and the failures. The settled successes (INDEXED,
# SKIPPED*, DELETED) are left out: counting them would page the whole table.
_OVERVIEW_STATUSES = tuple(
    s for s in ArticleStatus if s not in TERMINAL_STATUSES or s is ArticleStatus.FAILED_PERMANENT
)
_OVERVIEW_QUEUES = (
    ("ingest", "queues.ingest_url"),
    ("ingest_dlq", "queues.ingest_dlq_url"),
    ("backfill", "queues.backfill_url"),
    ("backfill_dlq", "queues.backfill_dlq_url"),
)


@router.get("/v1/ops/overview")
def ops_overview() -> Response[str]:
    """How many articles are in each in-progress or failed status, the one that has been there
    the longest, and the depth of each queue. A queue the role can't read doesn't fail the page;
    one this deployment has no URL for is left out."""
    store = aws.status_store()
    statuses: dict[str, dict[str, Any]] = {}
    for status in _OVERVIEW_STATUSES:
        oldest = store.oldest_by_status(status)
        statuses[status.value] = {
            "count": store.count_by_status(status),
            "oldest_at": (oldest.get("gsi1sk") or oldest.get("updated_at")) if oldest else None,
            "oldest_article_id": oldest["pk"] if oldest else None,
        }
    return json_response(
        200, {"statuses": statuses, "queues": _queue_depths(), "generated_at": iso_now()}
    )


def _queue_depths() -> dict[str, dict[str, Any]]:
    settings = get_settings()
    out: dict[str, dict[str, Any]] = {}
    for name, field in _OVERVIEW_QUEUES:
        url = str(settings.value(field))
        if not url:
            continue
        try:
            attrs = queue_attributes(url)
        except (ClientError, BotoCoreError) as err:
            logger.warning("queue attributes unavailable", extra={"queue": name, "error": str(err)})
            out[name] = {"error": "unavailable"}
            continue
        out[name] = {
            "visible": int(attrs.get("ApproximateNumberOfMessages", 0)),
            "in_flight": int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
        }
    return out
