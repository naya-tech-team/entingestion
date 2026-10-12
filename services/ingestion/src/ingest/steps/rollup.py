"""After an article reaches a terminal state: complete its process if everything is in."""

from __future__ import annotations

from ingest import events, producer
from ingest.status.store import StatusStore


def complete_process(store: StatusStore, process_id: str | None) -> str | None:
    if not process_id:
        return None
    status = store.complete_process_if_done(process_id, producer.process(process_id))
    if status:
        summary = store.summary(process_id) or {}
        events.emit(
            "process.ingested",
            {
                "process_id": process_id,
                "status": status,
                "indexed": int(summary.get("indexed", 0)),
                "skipped": int(summary.get("skipped", 0)),
                "failed": int(summary.get("failed", 0)),
            },
        )
    return status
