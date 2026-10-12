"""Reindex and rebuild (brief §7.3, UC9, UC10): a Distributed Map state machine re-runs indexed
articles from a staged step into a vector table, through the low-priority backfill queue.

    from_stage EMBED + a new table   -> model change: re-embed staged chunks into the new table
    from_stage INDEX + the live table -> rebuild: reload staged vectors, zero Bedrock calls

With a tenant_id, only that tenant's articles (a tenant moving to another model); without one,
the articles of the default registry (every tenant without its own registry).

    Plan    (reindex_plan)    -> the article ids, as a JSON array in staging (the Map's input)
    Enqueue (reindex_enqueue) -> one batch of ids -> BACKFILL messages on the backfill queue
"""

from __future__ import annotations

import json
from typing import Any

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, ConfigDict, Field

from ingest import aws, requeue
from ingest.config import get_settings
from ingest.models.status import ArticleStatus, StepName
from ingest.status.store import StatusStore
from ingest.tenancy import Tenancy
from ingest.vectorstore import registry
from ingest.vectorstore.targets import TARGETS

logger = Logger()
tracer = Tracer()

REINDEX_STAGES = (StepName.EMBED, StepName.INDEX)


class ReindexFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    since: str | None = Field(default=None, description="LATEST updated at or after (ISO-8601)")
    process_id: str | None = None
    pipeline_version: str | None = None
    only_uncovered: bool = Field(
        default=False, description="only articles whose current version isn't in the table yet"
    )


class ReindexRequest(BaseModel):
    """What the control API starts the state machine with."""

    model_config = ConfigDict(extra="forbid")

    filter: ReindexFilter = Field(default_factory=ReindexFilter)
    target_table: str
    from_stage: StepName = StepName.EMBED
    requested_by: str = "unknown"
    tenant_id: str | None = Field(default=None, min_length=1, max_length=128)

    def check(self) -> list[str]:
        problems = []
        if self.target_table not in TARGETS:
            problems.append(f"unknown target_table; known: {sorted(TARGETS)}")
        if self.from_stage not in REINDEX_STAGES:
            problems.append("from_stage must be EMBED (re-embed) or INDEX (rebuild)")
        return problems


def plan_items(
    store: StatusStore,
    f: ReindexFilter,
    target_table: str | None = None,
    tenant: str | None = None,
) -> list[str]:
    """Indexed articles matching the filter: `tenant`'s, or (None) the default registry's."""
    if f.process_id:
        items = [
            i
            for i in store.process_articles(f.process_id)
            if i.get("status") == ArticleStatus.INDEXED
            and (f.since is None or str(i.get("updated_at", "")) >= f.since)
        ]
    else:
        items = store.by_status(ArticleStatus.INDEXED, updated_since=f.since)
    if f.pipeline_version:
        items = [i for i in items if i.get("pipeline_version") == f.pipeline_version]
    if f.only_uncovered and target_table:
        attr = registry.version_attr(target_table)
        items = [i for i in items if i.get(attr) != i.get("doc_version")]
    items = Tenancy(store).of_tenant(items, tenant)
    return sorted({str(i["pk"]) for i in items})


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
def plan(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    request = ReindexRequest.model_validate(event["request"])
    ids = plan_items(aws.status_store(), request.filter, request.target_table, request.tenant_id)
    bucket = get_settings().staging_bucket
    key = f"_reindex/{event['execution_name']}/items.json"
    body = json.dumps([{"article_id": a} for a in ids]).encode()
    aws.s3().put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    logger.info(
        "reindex planned",
        extra={"count": len(ids), "target": request.target_table, "tenant": request.tenant_id},
    )
    return {"bucket": bucket, "key": key, "count": len(ids)}


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
def enqueue(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    batch = event["BatchInput"]
    store = aws.status_store()
    messages, missing = [], []
    for item in event["Items"]:
        message = requeue.message_for(
            store,
            str(item["article_id"]),
            trigger="BACKFILL",
            requested_by=str(batch.get("requested_by") or "reindex"),
            from_stage=StepName(batch["from_stage"]),
            target_table=str(batch["target_table"]),
        )
        if message is None or message.event == "REMOVE":
            missing.append(str(item["article_id"]))
        else:
            messages.append(message)
    queued = requeue.enqueue(messages, get_settings().backfill_queue_url)
    return {"queued": queued, "missing": missing}
