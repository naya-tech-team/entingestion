"""Build IngestMessages from the producer's current article row and put them on the ingest queue.

Used by the control API (MANUAL) and the reconciler (RECONCILER). The row is read at enqueue
time, so a re-run always processes the producer's current version of the article.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

from ingest import aws, producer
from ingest.config import get_settings
from ingest.models.messages import IngestMessage, Trigger
from ingest.models.status import ArticleStatus, StepName
from ingest.resume import resume_stage
from ingest.status.store import StatusStore

_SQS_BATCH = 10


def message_for(
    store: StatusStore,
    article_id: str,
    *,
    trigger: Trigger,
    requested_by: str | None = None,
    force: bool = False,
    from_stage: StepName | None = None,
    resume: bool = False,
    target_table: str | None = None,
) -> IngestMessage | None:
    """The message that re-runs an article, or None if neither side has it.

    resume: if the article didn't finish cleanly (failed, stuck, interrupted), start where the
    previous run's staged output is still intact (brief §10), else re-run it from Fetch. An
    article that's already indexed with the producer's content is left to the normal unchanged
    check. An explicit from_stage or force wins.
    """
    row = producer.article(article_id)
    latest = store.latest(article_id)
    if row is None:
        if latest is None or latest.get("status") == ArticleStatus.DELETED:
            return None
        # The producer deleted it but we still have it: run the delete path.
        return IngestMessage(
            article_id=article_id,
            event="REMOVE",
            trigger=trigger,
            requested_by=requested_by,
        )
    if resume and from_stage is None and not force and _needs_rerun(latest, row):
        from_stage = resume_stage(store, latest, row.get("content_sha256"))
        force = from_stage is None
    return IngestMessage(
        article_id=article_id,
        process_id=row.get("process_id"),
        event="UPSERT",
        s3_uri=row.get("s3_uri"),
        s3_version_id=row.get("s3_version_id"),
        content_sha256=row.get("content_sha256"),
        source_updated_at=row.get("updated_at"),
        tenant_id=row.get("tenant_id") or None,
        **producer.fingerprints(row),
        trigger=trigger,
        requested_by=requested_by,
        force=force,
        from_stage=from_stage,
        target_table=target_table,
    )


_DONE = frozenset({ArticleStatus.INDEXED, ArticleStatus.SKIPPED, ArticleStatus.SKIPPED_UNCHANGED})


def _needs_rerun(latest: dict[str, Any] | None, row: dict[str, Any]) -> bool:
    if latest is None:
        return False  # never seen: a normal first run
    done = latest.get("status") in _DONE
    return not (done and latest.get("content_sha256") == row.get("content_sha256"))


def enqueue(messages: Iterable[IngestMessage], queue_url: str | None = None) -> int:
    """Send messages to the ingest queue in batches. Returns how many were sent."""
    url = queue_url or get_settings().ingest_queue_url
    batch: list[IngestMessage] = []
    sent = 0
    for message in messages:
        batch.append(message)
        if len(batch) == _SQS_BATCH:
            sent += _send(url, batch)
            batch = []
    if batch:
        sent += _send(url, batch)
    return sent


def _send(url: str, batch: Sequence[IngestMessage]) -> int:
    entries: list[Any] = [
        {"Id": str(i), "MessageBody": json.dumps(m.model_dump(mode="json", exclude_none=True))}
        for i, m in enumerate(batch)
    ]
    resp = aws.sqs().send_message_batch(QueueUrl=url, Entries=entries)
    failed = resp.get("Failed") or []
    if failed:
        raise RuntimeError(f"{len(failed)} message(s) not queued: {failed[0].get('Message')}")
    return len(batch)
