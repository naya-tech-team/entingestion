"""Reconciler handler (brief §11), run hourly by EventBridge Scheduler. Every step is idempotent:
it only enqueues work (the dispatcher and Claim decide what actually runs) or moves an item whose
state it re-checks in a conditional write.

1. Expired leases -> re-queue, resuming where the staged output is intact.
2. FAILED_RETRYABLE past next_retry_at -> re-queue, or after MAX_RETRIES escalate to
   FAILED_PERMANENT (counted for the process, alert).
3. STAGED for over 30 minutes -> re-queue from Index.
4. Producer articles updated today or yesterday with no LATEST, or other content -> enqueue
   (catches dropped stream events, and a newer version whose run lost the lease).
5. Open process summaries -> completion check (§6.4).
6. Deletes still pending (their run never handed over) -> queue the REMOVE.
7. Weekly ({"drift": true}): every table any registry writes (the default one and each
   tenant's) against LATEST (version and chunk count, both ways), each article only against
   the tables of the registry that governs it (ingest.tenancy); mismatches are rebuilt by shadow
   runs on the backfill queue, rows of deleted or unknown articles removed. Rows in a table the
   article's registry no longer writes are left alone (a rollback may need them). Also, a page
   per status of articles whose producer row is gone.

Every run also publishes the backlog (BacklogCount / BacklogOldestSeconds per status, the SLO
metrics) and sends one digest of the articles that failed permanently since the last one.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext

from ingest import aws, events, producer, requeue, telemetry
from ingest.config import get_settings
from ingest.errors import IngestStale
from ingest.models.messages import IngestMessage
from ingest.models.status import ArticleStatus, RunOutcome, StepName
from ingest.status import keys
from ingest.status.store import Item, StatusStore, StepRef
from ingest.steps.rollup import complete_process
from ingest.tenancy import Tenancy
from ingest.timeutil import iso_at, now_epoch
from ingest.vectorstore import registry

logger = Logger()
tracer = Tracer()
metrics = Metrics()

MAX_RETRIES = 5
DIGEST_CURSOR = "ALERT#FAILED_DIGEST"
DIGEST_SAMPLES = 10
# Statuses of work in flight: their oldest age is the backlog SLO (docs/RUNBOOK.md "SLOs").
RUNNING = (
    ArticleStatus.CLAIMED,
    ArticleStatus.FETCHING,
    ArticleStatus.EXTRACTING,
    ArticleStatus.NORMALIZING,
    ArticleStatus.CHUNKING,
    ArticleStatus.EMBEDDING,
    ArticleStatus.VALIDATING,
    ArticleStatus.STAGED,
    ArticleStatus.INDEXING,
)
# Waiting on purpose (next_retry_at backs off for hours): published, but not in RUNNING.
BACKLOG = (*RUNNING, ArticleStatus.FAILED_RETRYABLE)
STAGED_GRACE_S = 30 * 60
STREAM_GRACE_S = 5 * 60  # a just-registered article may still be on its way through the Pipe
_COMPLETE = ("COMPLETE", "COMPLETE_WITH_ERRORS")
# Settled statuses whose producer row should still exist.
_GONE_SWEEP = (
    ArticleStatus.INDEXED,
    ArticleStatus.SKIPPED,
    ArticleStatus.STAGED,
    ArticleStatus.FAILED_RETRYABLE,
    ArticleStatus.FAILED_PERMANENT,
)


class Reconciler:
    def __init__(self, store: StatusStore, now: int, limit: int) -> None:
        self.store = store
        self.now = now
        self.limit = limit
        self.messages: dict[str, IngestMessage] = {}
        self.backfill: dict[tuple[str, str], IngestMessage] = {}  # shadow repairs
        self.counts: Counter[str] = Counter()

    def run(self, drift: bool = False) -> dict[str, int]:
        self.backlog()
        if drift:
            self.chunk_drift()
            self.producer_gone()
        self.pending_deletes()
        self.expired_leases()
        self.retryable()
        self.stuck_staged()
        self.producer_drift()
        self.complete_processes()
        self.counts["requeued"] = requeue.enqueue(self.messages.values()) if self.messages else 0
        if self.backfill:
            self.counts["requeued_backfill"] = requeue.enqueue(
                self.backfill.values(), get_settings().backfill_queue_url
            )
        self.failed_digest()  # last: includes this run's escalations
        return dict(self.counts)

    def backlog(self) -> None:
        """BacklogCount and BacklogOldestSeconds per status, and over all RUNNING statuses
        (status=RUNNING): the backlog SLO. Age = time since the article entered its status."""
        total, oldest = 0, 0
        for status in BACKLOG:
            count = self.store.count_by_status(status)
            first = self.store.oldest_by_status(status) if count else None
            age = self._age(str(first["gsi1sk"])) if first else 0
            telemetry.emit("BacklogCount", count, status=status.value)
            telemetry.emit("BacklogOldestSeconds", age, MetricUnit.Seconds, status=status.value)
            if status in RUNNING:
                total, oldest = total + count, max(oldest, age)
        telemetry.emit("BacklogCount", total, status="RUNNING")
        telemetry.emit("BacklogOldestSeconds", oldest, MetricUnit.Seconds, status="RUNNING")
        self.counts["backlog_running"] = total

    def _age(self, iso: str) -> int:
        try:
            since = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            return 0
        return max(0, self.now - int(since.timestamp()))

    def failed_digest(self) -> None:
        """One alert for the articles that became FAILED_PERMANENT since the last digest
        (MarkFailed and escalations): counts by error_code and a few article ids. The cursor
        lives in #CONFIG; the first run looks back one hour."""
        cursor = (self.store.get_config(DIGEST_CURSOR) or {}).get("since")
        since = str(cursor or iso_at(self.now - 3600))
        until = iso_at(self.now)
        items = self.store.by_status(
            ArticleStatus.FAILED_PERMANENT,
            updated_since=since,
            updated_before=until,
            limit=self.limit,
        )
        truncated = len(items) >= self.limit
        if items:
            by_code = Counter(str(i.get("error_code") or "unknown") for i in items)
            events.alert(
                f"Ingestion: {len(items)}{'+' if truncated else ''} article(s) failed permanently",
                {
                    "since": since,
                    "until": until,
                    "count": len(items),
                    "more_not_counted": truncated,
                    "by_error_code": dict(by_code.most_common()),
                    "sample_article_ids": [str(i["pk"]) for i in items[:DIGEST_SAMPLES]],
                    "next": "docs/RUNBOOK.md 'Failed articles': GET /v1/articles/{id}/status,"
                    ' fix the cause, then POST /v1/dlq/redrive {"error_code": ...}',
                },
            )
            self.counts["failed_digest"] = len(items)
        # A capped page continues where it stopped; otherwise the next digest starts here.
        next_since = str(items[-1]["gsi1sk"]) if truncated else until
        self.store.put_config(DIGEST_CURSOR, {"since": next_since})

    def _requeue(self, article_id: str, reason: str, **kwargs: Any) -> None:
        if article_id in self.messages:
            return
        message = requeue.message_for(self.store, article_id, trigger="RECONCILER", **kwargs)
        if message is not None:
            self.messages[article_id] = message
            self.counts[f"requeued_{reason}"] += 1

    def _remove(self, article_id: str, reason: str, *, force: bool = False) -> None:
        if article_id in self.messages:
            return
        self.messages[article_id] = IngestMessage(
            article_id=article_id, event="REMOVE", trigger="RECONCILER", force=force
        )
        self.counts[f"requeued_{reason}"] += 1

    def pending_deletes(self) -> None:
        """A delete arrived while a run held the article and the run never handed over (it
        crashed, or couldn't queue the REMOVE). Once its lease is gone, delete."""
        for item in self.store.pending_deletes(self.limit):
            if int(item.get("lease_expires_at", 0)) <= self.now:
                self._remove(str(item["pk"]), "pending_delete")

    def producer_gone(self) -> None:
        """Articles we still have whose producer row is gone (a lost REMOVE). A page per status
        per run; the cursor is kept so the sweep covers everything over time."""
        for status in _GONE_SWEEP:
            name = f"SWEEP#GONE#{status.value}"
            cursor = (self.store.get_config(name) or {}).get("cursor")
            items, next_key = self.store.page_by_status(
                status, json.loads(cursor) if cursor else None, self.limit
            )
            for item in items:
                if producer.article(str(item["pk"])) is None:
                    self._remove(str(item["pk"]), "producer_gone")
            self.store.put_config(name, {"cursor": json.dumps(next_key) if next_key else ""})

    def expired_leases(self) -> None:
        for item in self.store.expired_leases(self.now, self.limit):
            self._requeue(str(item["pk"]), "lease", resume=True)

    def retryable(self) -> None:
        for item in self.store.by_status(ArticleStatus.FAILED_RETRYABLE, limit=self.limit):
            if int(item.get("next_retry_at", 0)) > self.now:
                continue
            if int(item.get("retry_count", 0)) < MAX_RETRIES:
                self._requeue(str(item["pk"]), "retryable", resume=True)
            else:
                self._escalate(item)

    def _escalate(self, item: Item) -> None:
        article_id, process_id = str(item["pk"]), item.get("process_id")
        ref = StepRef(article_id, str(item["run_id"]), process_id)
        try:
            self.store.finish(
                ref,
                ArticleStatus.FAILED_PERMANENT,
                RunOutcome.FAILED_PERMANENT,
                require=ArticleStatus.FAILED_RETRYABLE,
            )
        except IngestStale:
            return  # another run took it meanwhile
        self.counts["escalated"] += 1
        # Reported in this run's digest (failed_digest), like MarkFailed's permanent failures.
        telemetry.emit("ArticlesFailedPermanent", 1, error_code=str(item.get("error_code")))
        complete_process(self.store, process_id)

    def stuck_staged(self) -> None:
        cutoff = iso_at(self.now - STAGED_GRACE_S)
        for item in self.store.by_status(
            ArticleStatus.STAGED, updated_before=cutoff, limit=self.limit
        ):
            self._requeue(str(item["pk"]), "staged", from_stage=StepName.INDEX)

    def producer_drift(self) -> None:
        today = datetime.fromtimestamp(self.now, UTC).date()
        recent = iso_at(self.now - STREAM_GRACE_S)
        for day in (today, today - timedelta(days=1)):
            for row in producer.updated_on(day.isoformat()):
                if len(self.messages) >= self.limit:
                    return
                if str(row.get("updated_at", "")) > recent:
                    continue
                latest = self.store.latest(str(row["article_id"]))
                if latest is not None and (
                    latest.get("content_sha256") == row.get("content_sha256")
                    or int(latest.get("lease_expires_at", 0)) > self.now
                ):
                    continue  # in sync, or a run is working on it right now
                self._requeue(str(row["article_id"]), "drift")

    def chunk_drift(self) -> None:
        """Every write table against LATEST, both ways, a capped page at a time (cursors in
        #CONFIG): rows of a different version or count are rebuilt, rows of deleted or unknown
        articles are removed, and indexed articles missing from a table are written into it.
        Repairs are shadow runs on the backfill queue, so they never touch the live status.
        Each article is checked against its registry's tables only (its tenant's own registry,
        or the default one)."""
        tenancy = Tenancy(self.store)
        tables = sorted({t for r in tenancy.registries.values() for t in r.writes})
        for table in tables:
            self._drift_rows(table, tenancy)
            self._drift_coverage(table, tenancy)

    def _drift_rows(self, table: str, tenancy: Tenancy) -> None:
        name = f"SWEEP#DRIFT#{table}"
        after = str((self.store.get_config(name) or {}).get("after") or "")
        seen = 0
        while seen < self.limit:
            page = self._query({"action": "versions", "table": table, "after": after})
            for article_id, version, count in page["rows"]:
                latest = self.store.latest(article_id)
                if latest is None or latest.get("status") == ArticleStatus.DELETED:
                    self._remove(article_id, "drift_orphan", force=True)
                    continue
                if latest.get("status") != ArticleStatus.INDEXED:
                    continue
                tables = tenancy.registry_of(tenancy.tenants([latest])[article_id])
                if table in tables.writes and (
                    version != int(latest.get("doc_version", 0))
                    or count != int(latest.get("chunk_count", 0))
                ):
                    self._repair(article_id, table, live=table == tables.live)
            seen += len(page["rows"])
            after = page["next"] or ""
            if not after:
                break
        self.store.put_config(name, {"after": after})

    def _drift_coverage(self, table: str, tenancy: Tenancy) -> None:
        name = f"SWEEP#COVER#{table}"
        cursor = (self.store.get_config(name) or {}).get("cursor")
        items, next_key = self.store.page_by_status(
            ArticleStatus.INDEXED, json.loads(cursor) if cursor else None, self.limit
        )
        attr = registry.version_attr(table)
        gaps = [i for i in items if i.get(attr) != i.get("doc_version")]
        tenants = tenancy.tenants(gaps)
        for item in gaps:
            tables = tenancy.registry_of(tenants[str(item["pk"])])
            if table in tables.writes:
                self._repair(str(item["pk"]), table, live=table == tables.live)
        self.store.put_config(name, {"cursor": json.dumps(next_key) if next_key else ""})

    def _repair(self, article_id: str, table: str, *, live: bool) -> None:
        """Rewrite the article's current version into one table. The live table's vectors are
        always staged (rebuild from INDEX); a newer table's may not be (re-embed, cache first)."""
        if (article_id, table) in self.backfill:
            return
        message = requeue.message_for(
            self.store,
            article_id,
            trigger="RECONCILER",
            from_stage=StepName.INDEX if live else StepName.EMBED,
            target_table=table,
        )
        if message is not None and message.event == "UPSERT":
            self.backfill[(article_id, table)] = message
            self.counts["requeued_drift_repair"] += 1

    def _query(self, payload: dict[str, Any]) -> dict[str, Any]:
        resp = aws.lambda_client().invoke(
            FunctionName=get_settings().query_function_name,
            Payload=json.dumps(payload).encode(),
        )
        out = json.loads(resp["Payload"].read())
        if resp.get("FunctionError"):
            raise RuntimeError(f"query {payload['action']} failed: {out.get('errorMessage', out)}")
        return dict(out)

    def complete_processes(self) -> None:
        for summary in self.store.open_summaries(self.limit):
            process_id = str(summary["pk"]).removeprefix("PROCESS#")
            if complete_process(self.store, process_id):
                self.counts["processes_completed"] += 1
            current = self.store.get(keys.summary_key(process_id)) or {}
            if current.get("status") in _COMPLETE:
                self.store.close_summary(process_id)


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, int]:
    settings = get_settings()
    reconciler = Reconciler(aws.status_store(), now_epoch(), settings.reconcile_batch)
    counts = reconciler.run(drift=bool(event.get("drift")))
    for name in ("requeued", "escalated", "processes_completed", "requeued_drift_repair"):
        metrics.add_metric(
            name=f"Reconciler_{name}", unit=MetricUnit.Count, value=counts.get(name, 0)
        )
    logger.info("reconciled", extra=counts)
    return counts
