"""Step 1: Claim (brief §4.4). Decides what this run does; recorded on RUN#, no step item."""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext

from ingest import aws
from ingest.models.messages import IngestMessage
from ingest.steps.rollup import complete_process

logger = Logger()
tracer = Tracer()

# Claim decisions that end the run (the state machine goes to Done or Stale).
_ENDS_HERE = frozenset({"SKIP", "STALE", "DONE"})


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    ctx = event["ctx"]
    message = IngestMessage.model_validate(ctx["message"])
    run_id = str(ctx["run_id"])
    logger.append_keys(article_id=message.article_id, run_id=run_id, process_id=message.process_id)

    slot = ctx.get("slot")
    store = aws.status_store()
    result = store.claim(message, run_id, event.get("execution_arn"), slot)
    logger.info(
        "claimed",
        extra={"action": result.action, "claim": result.claim, "doc_version": result.doc_version},
    )
    if result.action in _ENDS_HERE:
        store.release_slot(slot, run_id)  # nothing more to run: admit the next one
    if result.action == "SKIP" and result.lane is None:
        complete_process(store, message.process_id)

    return {
        "slot": slot,
        "action": result.action,
        "article_id": message.article_id,
        "run_id": run_id,
        "doc_version": result.doc_version or 0,
        "staging_prefix": result.staging_prefix or "",
        "start_at": result.start_at.value if result.start_at else None,
        "mode": result.action if result.action in ("DELETE", "METADATA") else "UPSERT",
        "lane": result.lane,
        "targets": list(result.targets),
        "message": ctx["message"],
    }
