"""A run that finds a pending delete hands the article over: it releases the lease and queues
the REMOVE, which then claims the article and deletes it. If queuing fails, the reconciler's
pending-delete sweep queues it instead."""

from __future__ import annotations

from aws_lambda_powertools import Logger

from ingest import requeue
from ingest.errors import IngestStale
from ingest.models.messages import IngestMessage
from ingest.status.store import StatusStore, StepRef

logger = Logger()


def hand_over_delete(store: StatusStore, ref: StepRef) -> None:
    try:
        store.yield_to_delete(ref)
    except IngestStale:
        return  # another run holds the article now; it will meet the marker too
    remove = IngestMessage(
        article_id=ref.article_id, event="REMOVE", trigger="RECONCILER", requested_by="handover"
    )
    try:
        requeue.enqueue([remove])
    except Exception:
        logger.warning("couldn't queue the handed-over delete", exc_info=True)
    logger.info("handed the article over to a pending delete")
