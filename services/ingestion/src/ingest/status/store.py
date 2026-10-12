"""Reads and writes on ingestion_status (brief §6).

Every write that moves an article between states is a DynamoDB transaction with a condition on
LATEST.run_id, so a run that lost its lease can't overwrite the run that owns the article
(IngestStale). Transactions go through the resource's client, which serializes plain Python
values itself (pre-serialized {"S": ...} values would be double-wrapped).
"""

from __future__ import annotations

import random
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, cast

from botocore.exceptions import ClientError

from ingest.config import Settings
from ingest.errors import DeletePending, ErrorCode, IngestError, IngestStale, make_error
from ingest.ids import staging_prefix
from ingest.models.messages import IngestMessage
from ingest.models.status import (
    ROLLUP_COUNTER,
    STEP_STATUS,
    STEP_SUCCESS_STATUS,
    TERMINAL_STATUSES,
    ArticleStatus,
    ClaimKind,
    RunOutcome,
    StepName,
    StepStatus,
)
from ingest.status import keys
from ingest.timeutil import days, iso_now, now_epoch
from ingest.vectorstore import registry
from ingest.vectorstore.targets import TARGETS

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

Item = dict[str, Any]
MAX_ATTEMPTS_KEPT = 5
_ROLLUP_RETRIES = 3

# An unchanged article that already reached one of these is skipped (brief §4.4 Claim).
_SETTLED_STATUSES = frozenset(
    {ArticleStatus.INDEXED, ArticleStatus.SKIPPED, ArticleStatus.FAILED_PERMANENT}
)


def plain(value: Any) -> Any:
    """DynamoDB Decimals back to int/float."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def _cancellation_codes(err: ClientError) -> list[str] | None:
    if err.response.get("Error", {}).get("Code") != "TransactionCanceledException":
        return None
    return [str(r.get("Code", "None")) for r in err.response.get("CancellationReasons") or []]


# Concurrent transactions on one item (e.g. a process SUMMARY) cancel each other with
# TransactionConflict; that's contention, not a failed condition, so retry it.
_CONFLICT_ATTEMPTS = 6
_CONFLICT_BASE_S = 0.05

# Other cancellation reasons: capacity is transient and throttled; a malformed or oversized
# write is a bug that no retry fixes.
_CANCEL_THROTTLED = frozenset(
    {"ThrottlingError", "ProvisionedThroughputExceeded", "RequestLimitExceeded"}
)
_CANCEL_BUG = frozenset({"ValidationError", "ItemCollectionSizeLimitExceeded"})


def _cancellation_error(codes: list[str]) -> IngestError:
    reasons = set(codes) - {"None"}
    detail = f"transaction cancelled: {codes}"
    if reasons & _CANCEL_BUG:
        return make_error(ErrorCode.BUG, detail)
    if reasons & _CANCEL_THROTTLED:
        return make_error(ErrorCode.THROTTLED, detail)
    return make_error(ErrorCode.UPSTREAM_5XX, detail)


def _is_condition_failure(err: ClientError) -> bool:
    return err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _claim_record(result: ClaimResult) -> Item:
    """The decision written on RUN#, so a retried Claim can repeat it."""
    return _drop_none(
        {
            "action": result.action,
            "doc_version": result.doc_version,
            "start_at": result.start_at.value if result.start_at else None,
            "staging_prefix": result.staging_prefix,
            "lane": result.lane,
            "targets": list(result.targets) or None,
        }
    )


def _recorded_claim(run: Item) -> ClaimResult:
    claim = run.get("claim")
    return ClaimResult(
        run["action"],
        ClaimKind(claim) if claim else None,
        int(run["doc_version"]) if run.get("doc_version") is not None else None,
        StepName(run["start_at"]) if run.get("start_at") else None,
        run.get("staging_prefix"),
        run.get("lane"),
        tuple(run.get("targets") or ()),
    )


def _older_than_latest(msg: IngestMessage, latest: Item) -> bool:
    """The message carries an older producer update than LATEST already has, or one no newer
    than the delete that ended the article. Producer timestamps are fixed-width ISO-8601 UTC,
    so they compare as strings."""
    mine, theirs = msg.source_updated_at, latest.get("source_updated_at")
    if not mine or not theirs:
        return False
    if latest.get("status") == ArticleStatus.DELETED or "delete_requested_at" in latest:
        return bool(mine <= theirs)
    return bool(mine < theirs)


def _drop_none(item: Mapping[str, Any]) -> Item:
    return {k: v for k, v in item.items() if v is not None}


def _title_changed(msg: IngestMessage, latest: Item) -> bool:
    """A new title is a content change: it's part of every chunk's embed_text. Unknown (an
    article indexed before titles were fingerprinted) counts as unchanged."""
    theirs = latest.get("title_sha256")
    return msg.title_sha256 is not None and theirs is not None and theirs != msg.title_sha256


def _set_tenant(msg: IngestMessage, sets: list[str], values: dict[str, Any]) -> str:
    """Record the run's tenant on LATEST (SET), or drop a stale one (returns " REMOVE ...")."""
    if msg.tenant_id:
        sets.append("tenant_id = :tenant")
        values[":tenant"] = msg.tenant_id
        return ""
    return " REMOVE tenant_id"


def _fingerprints(msg: IngestMessage) -> dict[str, str]:
    """The message's title/metadata fingerprints, for LATEST (only those it carries)."""
    return {
        k: v
        for k, v in (("meta_sha256", msg.meta_sha256), ("title_sha256", msg.title_sha256))
        if v is not None
    }


@dataclass(frozen=True)
class ClaimResult:
    action: Literal["RUN", "SKIP", "STALE", "DELETE", "DONE", "METADATA"]
    claim: ClaimKind | None = None
    doc_version: int | None = None
    start_at: StepName | None = None
    staging_prefix: str | None = None
    lane: str | None = None
    targets: tuple[str, ...] = ()


@dataclass(frozen=True)
class StepRef:
    """Identifies the run a step belongs to. lane: the vector table of a shadow run (its status
    lives on SHADOW#<table>, and it may only work on LATEST's doc_version). slot: the admission
    slot the dispatcher gave the run (#SLOTS/<slot>), released when the run ends."""

    article_id: str
    run_id: str
    process_id: str | None
    lane: str | None = None
    doc_version: int | None = None
    slot: str | None = None


class StatusStore:
    def __init__(self, table: Table, settings: Settings) -> None:
        self.table = table
        self.client = table.meta.client
        self.settings = settings

    # -- reads --------------------------------------------------------------

    def get(self, key: Mapping[str, str]) -> Item | None:
        item = self.table.get_item(Key=dict(key), ConsistentRead=True).get("Item")
        return cast(Item, plain(item)) if item else None

    def latest(self, article_id: str) -> Item | None:
        return self.get(keys.latest_key(article_id))

    def run_items(self, article_id: str, run_id: str) -> list[Item]:
        """The RUN# item and its step items."""
        resp = self.table.query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :run)",
            ExpressionAttributeValues={":pk": article_id, ":run": keys.run_sk(run_id)},
            ConsistentRead=True,
        )
        return [cast(Item, plain(i)) for i in resp.get("Items", [])]

    def recent_runs(self, article_id: str, limit: int = 5) -> list[Item]:
        """The newest RUN# items (run ids are ULIDs, so they sort by start time)."""
        items = self._query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :run)",
            ExpressionAttributeValues={":pk": article_id, ":run": "RUN#"},
            ScanIndexForward=False,
        )
        return [i for i in items if "#STEP#" not in str(i["sk"])][:limit]

    def by_status(
        self,
        status: ArticleStatus,
        *,
        updated_before: str | None = None,
        updated_since: str | None = None,
        limit: int | None = None,
    ) -> list[Item]:
        """LATEST items in a status (gsi1-status), oldest first: updated_since <= updated_at <
        updated_before."""
        condition, values = "gsi1pk = :st", {":st": status.value}
        if updated_before is not None and updated_since is not None:
            condition += " AND gsi1sk BETWEEN :since AND :before"  # inclusive: dropped below
            values |= {":since": updated_since, ":before": updated_before}
        elif updated_before is not None:
            condition += " AND gsi1sk < :before"
            values[":before"] = updated_before
        elif updated_since is not None:
            condition += " AND gsi1sk >= :since"
            values[":since"] = updated_since
        items = self._query(
            limit=limit,
            IndexName="gsi1-status",
            KeyConditionExpression=condition,
            ExpressionAttributeValues=values,
        )
        if updated_before is not None and updated_since is not None:
            items = [i for i in items if str(i.get("gsi1sk")) != updated_before]
        return items

    def count_by_status(self, status: ArticleStatus) -> int:
        """How many LATEST items are in a status (gsi1-status, COUNT only)."""
        kwargs: dict[str, Any] = {
            "IndexName": "gsi1-status",
            "KeyConditionExpression": "gsi1pk = :st",
            "ExpressionAttributeValues": {":st": status.value},
            "Select": "COUNT",
        }
        count = 0
        while True:
            resp = self.table.query(**kwargs)
            count += int(resp.get("Count", 0))
            start = resp.get("LastEvaluatedKey")
            if not start:
                return count
            kwargs["ExclusiveStartKey"] = start

    def oldest_by_status(self, status: ArticleStatus) -> Item | None:
        """The LATEST item that has been in a status the longest (gsi1sk = when it got there)."""
        items = self.by_status(status, limit=1)
        return items[0] if items else None

    def page_by_status(
        self, status: ArticleStatus, start: dict[str, Any] | None, limit: int
    ) -> tuple[list[Item], dict[str, Any] | None]:
        """One page of LATEST items in a status, and the key to continue from (None at the end).
        For sweeps that go through a whole status a little at a time."""
        kwargs: dict[str, Any] = {
            "IndexName": "gsi1-status",
            "KeyConditionExpression": "gsi1pk = :st",
            "ExpressionAttributeValues": {":st": status.value},
            "Limit": limit,
        }
        if start:
            kwargs["ExclusiveStartKey"] = start
        resp = self.table.query(**kwargs)
        items = [cast(Item, plain(i)) for i in resp.get("Items", [])]
        return items, cast("dict[str, Any] | None", plain(resp.get("LastEvaluatedKey")))

    def get_config(self, name: str) -> Item | None:
        return self.get(keys.config_key(name))

    def configs(self, prefix: str) -> list[Item]:
        """#CONFIG items whose name starts with `prefix` (e.g. every tenant's registry)."""
        return self._query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={":pk": keys.CONFIG, ":prefix": prefix},
            ConsistentRead=True,
        )

    def put_config(self, name: str, attrs: Mapping[str, Any]) -> None:
        self.table.put_item(Item={**keys.config_key(name), **attrs, "updated_at": iso_now()})

    def expired_leases(self, now: int, limit: int | None = None) -> list[Item]:
        """LATEST items whose run stopped extending its lease (gsi3-lease)."""
        return self._query(
            limit=limit,
            IndexName="gsi3-lease",
            KeyConditionExpression="gsi3pk = :b AND gsi3sk <= :now",
            ExpressionAttributeValues={":b": keys.LEASE_BUCKET, ":now": now},
        )

    def process_articles(self, process_id: str) -> list[Item]:
        """LATEST items last touched by a process (gsi2-process)."""
        return self._query(
            IndexName="gsi2-process",
            KeyConditionExpression="gsi2pk = :pid",
            ExpressionAttributeValues={":pid": process_id},
        )

    def open_summaries(self, limit: int | None = None) -> list[Item]:
        """SUMMARY items marked open: their process may still need its completion check."""
        return self._query(
            limit=limit,
            IndexName="gsi1-status",
            KeyConditionExpression="gsi1pk = :open",
            ExpressionAttributeValues={":open": keys.SUMMARY_OPEN},
        )

    def close_summary(self, process_id: str) -> None:
        """Drop the open marker of a completed process (a later count change sets it again)."""
        self.table.update_item(
            Key=keys.summary_key(process_id),
            UpdateExpression="REMOVE gsi1pk, gsi1sk",
            ConditionExpression="attribute_exists(pk)",
        )

    # -- admission slots (dispatcher backpressure) ---------------------------

    def acquire_slot(self, lane: str, size: int, run_id: str) -> str | None:
        """Take a free or expired slot of the lane for run_id; None if all `size` are held.

        A slot is held until the run ends (release_slot) or its lease runs out: runs extend it
        at every step, so only a run that died without ending (or one the state machine ended
        quietly as stale) leaves a slot behind, and only for one lease."""
        now = now_epoch()
        resp = self.table.query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :lane)",
            ExpressionAttributeValues={":pk": keys.SLOTS, ":lane": f"{lane}#"},
            ConsistentRead=True,
        )
        items = [cast(Item, plain(i)) for i in resp.get("Items", [])]
        held = {str(i["sk"]): int(i.get("expires_at", 0)) for i in items if "run_id" in i}
        free = [keys.slot_sk(lane, i) for i in range(size)]
        free = [s for s in free if held.get(s, 0) <= now]
        random.shuffle(free)  # spread contending dispatchers over the free slots
        for slot in free:
            try:
                self.table.put_item(
                    Item={
                        **keys.slot_key(slot),
                        "lane": lane,
                        "run_id": run_id,
                        "expires_at": now + self.settings.lease_seconds,
                        "acquired_at": iso_now(),
                    },
                    ConditionExpression="attribute_not_exists(run_id) OR expires_at <= :now",
                    ExpressionAttributeValues={":now": now},
                )
            except ClientError as err:
                if _is_condition_failure(err):
                    continue  # another dispatcher took it first
                raise
            return slot
        return None

    def extend_slot(self, ref: StepRef) -> None:
        """Keep the run's slot while it works (every step start and heartbeat). Best effort: a
        slot that expired and went to another run stays with that run."""
        if not ref.slot:
            return
        try:
            self.table.update_item(
                Key=keys.slot_key(ref.slot),
                UpdateExpression="SET expires_at = :exp",
                ConditionExpression="run_id = :run",
                ExpressionAttributeValues={
                    ":exp": now_epoch() + self.settings.lease_seconds,
                    ":run": ref.run_id,
                },
            )
        except ClientError:
            return  # taken over, or DynamoDB refused: the slot's lease decides

    def release_slot(self, slot: str | None, run_id: str) -> None:
        """Free the slot if run_id still holds it. Idempotent, and never fails the run: a slot
        that can't be released now frees itself when its lease runs out."""
        if not slot:
            return
        try:
            self.table.update_item(
                Key=keys.slot_key(slot),
                UpdateExpression="SET released_at = :ts REMOVE run_id, expires_at",
                ConditionExpression="run_id = :run",
                ExpressionAttributeValues={":ts": iso_now(), ":run": run_id},
            )
        except ClientError:
            return  # already free or taken over (or DynamoDB refused: the lease frees it)

    def _release(self, ref: StepRef) -> None:
        self.release_slot(ref.slot, ref.run_id)

    # -- helpers ------------------------------------------------------------

    def _query(self, *, limit: int | None = None, **kwargs: Any) -> list[Item]:
        items: list[Item] = []
        while True:
            resp = self.table.query(**kwargs)
            items += [cast(Item, plain(i)) for i in resp.get("Items", [])]
            if limit is not None and len(items) >= limit:
                return items[:limit]
            start = resp.get("LastEvaluatedKey")
            if not start:
                return items
            kwargs["ExclusiveStartKey"] = start

    def _transact(self, items: list[dict[str, Any]]) -> list[str] | None:
        """Run a transaction. Returns None on success, or the per-item codes when a condition
        failed. Conflicts with concurrent transactions are retried; other cancellations raise."""
        for attempt in range(_CONFLICT_ATTEMPTS):
            try:
                self.client.transact_write_items(TransactItems=items)  # type: ignore[arg-type]
            except ClientError as err:
                codes = _cancellation_codes(err)
                if codes is None:
                    raise
                if "ConditionalCheckFailed" in codes:
                    return codes
                if set(codes) - {"None"} != {"TransactionConflict"}:
                    raise _cancellation_error(codes) from err
                time.sleep(random.uniform(0, _CONFLICT_BASE_S * 2**attempt))  # noqa: S311
            else:
                return None
        raise make_error(ErrorCode.DB_CONFLICT, "transaction conflict persisted")

    def _status_sets(
        self, status: ArticleStatus, article_id: str, ts: str
    ) -> tuple[list[str], dict[str, Any]]:
        """SET clauses that keep status and its sparse GSI keys in sync (gsi1, gsi2)."""
        return (
            ["#status = :st", "updated_at = :ts", "gsi1pk = :st", "gsi1sk = :ts", "gsi2sk = :g2"],
            {":st": status.value, ":ts": ts, ":g2": keys.gsi2_sk(status, article_id)},
        )

    def _run_ttl(self, now: int) -> int:
        return now + days(self.settings.run_ttl_days)

    def _step_ttl(self, now: int) -> int:
        return now + days(self.settings.step_ttl_days)

    # -- claim (brief §4.4 step 1) -----------------------------------------

    def claim(
        self,
        msg: IngestMessage,
        run_id: str,
        execution_arn: str | None,
        slot: str | None = None,
    ) -> ClaimResult:
        now, ts = now_epoch(), iso_now()
        latest = self.latest(msg.article_id)
        run = _drop_none(
            {
                "slot": slot,
                **keys.run_key(msg.article_id, run_id),
                "run_id": run_id,
                "event": msg.event,
                "trigger": msg.trigger,
                "requested_by": msg.requested_by,
                "from_stage": msg.from_stage.value if msg.from_stage else None,
                "force": msg.force or None,
                "process_id": msg.process_id,
                "content_sha256": msg.content_sha256,
                "pipeline_version": self.settings.pipeline_version,
                "embedding_model": self._run_model(msg),
                "tenant_id": msg.tenant_id,
                "execution_arn": execution_arn,
                "started_at": ts,
                "expires_at_epoch": self._run_ttl(now),
            }
        )

        # A retried Claim (its response was lost) repeats the decision it already recorded.
        recorded = self.get(keys.run_key(msg.article_id, run_id))
        if recorded is not None and "action" in recorded:
            return _recorded_claim(recorded)
        if msg.event == "REMOVE":
            return self._claim_delete(msg, run, latest, now, ts)
        if msg.target_table:
            return self._claim_shadow(msg, run, latest, now, ts)
        return self._decide(msg, run, latest, now, ts)

    def _decide(
        self, msg: IngestMessage, run: Item, latest: Item | None, now: int, ts: str
    ) -> ClaimResult:
        run_id = str(run["run_id"])
        if latest is None:
            return self._claim_new(msg, run, now, ts)
        if _older_than_latest(msg, latest):
            return self._claim_superseded(msg, run, ts)
        if not self._lease_free(latest, run_id, now):
            self._put_run(run, ClaimKind.CHANGED, RunOutcome.STALE, ts)
            return ClaimResult("STALE")
        if self._unchanged(msg, latest) and not self._moved_registry(msg, latest):
            if self._metadata_changed(msg, latest):
                return self._claim_metadata(msg, run, latest, now, ts)
            return self._claim_unchanged(msg, run, latest, ts)
        return self._claim_changed(msg, run, latest, now, ts)

    def _head_key(self, ref: StepRef) -> dict[str, str]:
        """The item that tracks this run: LATEST, or SHADOW#<table> for a shadow run."""
        if ref.lane:
            return keys.shadow_key(ref.article_id, ref.lane)
        return keys.latest_key(ref.article_id)

    def _lease_free(self, latest: Item, run_id: str, now: int) -> bool:
        expires = latest.get("lease_expires_at")
        return expires is None or int(expires) <= now or latest.get("lease_owner") == run_id

    def _unchanged(self, msg: IngestMessage, latest: Item) -> bool:
        return (
            not msg.is_replay
            and latest.get("content_sha256") == msg.content_sha256
            and not _title_changed(msg, latest)
            and latest.get("pipeline_version") == self.settings.pipeline_version
            and latest.get("status") in _SETTLED_STATUSES
        )

    @staticmethod
    def _metadata_changed(msg: IngestMessage, latest: Item) -> bool:
        """Same content, but the row's metadata or tenant changed (or was never fingerprinted)
        and the article has rows to update."""
        return (
            msg.meta_sha256 is not None
            and latest.get("meta_sha256") != msg.meta_sha256
            and latest.get("status") == ArticleStatus.INDEXED
            and "delete_requested_at" not in latest
        )

    def _put_run(
        self,
        run: Item,
        claim: ClaimKind,
        outcome: RunOutcome,
        ts: str,
        result: ClaimResult | None = None,
    ) -> None:
        action = result or ClaimResult("STALE" if outcome is RunOutcome.STALE else "DONE", claim)
        self.table.put_item(
            Item={
                **run,
                **_claim_record(action),
                "claim": claim.value,
                "outcome": outcome.value,
                "ended_at": ts,
            }
        )

    def _write_tables(self, msg: IngestMessage) -> tuple[str, ...]:
        """The tables a live run of the article writes: its tenant's registry."""
        return registry.load(self, msg.tenant_id).writes

    def _run_model(self, msg: IngestMessage) -> str | None:
        """The model of the table that decides the run (recorded on RUN#)."""
        if msg.event == "REMOVE":
            return None
        table = msg.target_table or registry.load(self, msg.tenant_id).live
        return TARGETS[table].model_id if table in TARGETS else None

    def _moved_registry(self, msg: IngestMessage, latest: Item) -> bool:
        """The article's tenant changed to one whose registry writes other tables: its vectors
        must be made for those tables (a re-run), not only its metadata rewritten. Unknown for
        articles indexed before LATEST recorded the tenant."""
        if "tenant_id" not in latest or latest.get("status") != ArticleStatus.INDEXED:
            return False
        before, after = registry.scope(str(latest["tenant_id"])), registry.scope(msg.tenant_id)
        if before == after:
            return False
        return set(registry.load(self, before).writes) != set(self._write_tables(msg))

    def _claim_shadow(
        self, msg: IngestMessage, run: Item, latest: Item | None, now: int, ts: str
    ) -> ClaimResult:
        """A run for one vector table only (reindex, rebuild, drift repair): its own status item,
        never LATEST, so it can't hold up or overwrite the article's live status. It works on
        the article's current indexed version and nothing else."""
        table = str(msg.target_table)
        start = msg.from_stage or StepName.EMBED
        current = (
            latest is not None
            and latest.get("status") == ArticleStatus.INDEXED
            and "delete_requested_at" not in latest
            and latest.get("content_sha256") == msg.content_sha256
        )
        if not current or latest is None:
            # Nothing to do: the live run will write the newer content to every write table.
            self._put_run(run, ClaimKind.UNCHANGED, RunOutcome.SUPERSEDED, ts, ClaimResult("SKIP"))
            return ClaimResult("SKIP")
        doc_version = int(latest["doc_version"])
        prefix = str(latest["staging_prefix"])
        result = ClaimResult(
            "RUN", ClaimKind.UNCHANGED, doc_version, start, prefix, table, (table,)
        )
        lease = self._lease_values(run["run_id"], now)
        shadow = {
            **keys.shadow_key(msg.article_id, table),
            "article_id": msg.article_id,
            "table_name": table,
            "status": ArticleStatus.CLAIMED.value,
            "stage": (start.previous or StepName.CLAIM).value,
            "run_id": run["run_id"],
            "doc_version": doc_version,
            "staging_prefix": prefix,
            "updated_at": ts,
            "lease_owner": run["run_id"],
            "lease_expires_at": lease["lease_expires_at"],
        }
        codes = self._transact(
            [
                {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": shadow,
                        "ConditionExpression": (
                            "attribute_not_exists(pk) OR attribute_not_exists(lease_expires_at)"
                            " OR lease_expires_at <= :now"
                        ),
                        "ExpressionAttributeValues": {":now": now},
                    }
                },
                self._latest_is_version(msg.article_id, doc_version),
                {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": {**run, **_claim_record(result), "claim": "UNCHANGED"},
                    }
                },
            ]
        )
        if codes is not None:
            self._put_run(run, ClaimKind.UNCHANGED, RunOutcome.STALE, ts)
            return ClaimResult("STALE")
        return result

    def _latest_is_version(self, article_id: str, doc_version: int) -> dict[str, Any]:
        """Transaction check: LATEST still holds this version and no delete is waiting."""
        return {
            "ConditionCheck": {
                "TableName": self.table.name,
                "Key": keys.latest_key(article_id),
                "ConditionExpression": (
                    "doc_version = :v AND #status <> :deleted"
                    " AND attribute_not_exists(delete_requested_at)"
                ),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {
                    ":v": doc_version,
                    ":deleted": ArticleStatus.DELETED.value,
                },
            }
        }

    def record_table_version(self, article_id: str, table: str, doc_version: int) -> None:
        """LATEST.tv_<table> = doc_version once that version is in `table` (switch coverage).
        Ignored if LATEST has moved on to another version meanwhile."""
        try:
            self.table.update_item(
                Key=keys.latest_key(article_id),
                UpdateExpression="SET #tv = :v",
                ConditionExpression="doc_version = :v",
                ExpressionAttributeNames={"#tv": registry.version_attr(table)},
                ExpressionAttributeValues={":v": doc_version},
            )
        except ClientError as err:
            if not _is_condition_failure(err):
                raise

    def _claim_superseded(self, msg: IngestMessage, run: Item, ts: str) -> ClaimResult:
        """An older update than the article already has (a late delivery or a DLQ redrive):
        nothing to do, but the process still counts the article as settled."""
        result = ClaimResult("SKIP", ClaimKind.CHANGED)
        run_item = {
            **run,
            **_claim_record(result),
            "claim": ClaimKind.CHANGED.value,
            "outcome": RunOutcome.SUPERSEDED.value,
            "ended_at": ts,
        }
        self._with_rollup(
            [{"Put": {"TableName": self.table.name, "Item": run_item}}],
            process_id=msg.process_id,
            article_id=msg.article_id,
            counter="skipped",
            run_id=run["run_id"],
            ts=ts,
            latest_index=None,
            first_only=True,
        )
        return result

    def _lease_values(self, run_id: str, now: int) -> dict[str, Any]:
        expires = now + self.settings.lease_seconds
        return {
            "lease_owner": run_id,
            "lease_expires_at": expires,
            "gsi3pk": keys.LEASE_BUCKET,
            "gsi3sk": expires,
        }

    def _claim_new(self, msg: IngestMessage, run: Item, now: int, ts: str) -> ClaimResult:
        doc_version = 1
        prefix = staging_prefix(msg.article_id, doc_version)
        result = ClaimResult(
            "RUN",
            ClaimKind.NEW,
            doc_version,
            StepName.FETCH,
            prefix,
            None,
            self._write_tables(msg),
        )
        latest = _drop_none(
            {
                **keys.latest_key(msg.article_id),
                "article_id": msg.article_id,
                "status": ArticleStatus.CLAIMED.value,
                "stage": StepName.CLAIM.value,
                "run_id": run["run_id"],
                "doc_version": doc_version,
                "content_sha256": msg.content_sha256,
                **_fingerprints(msg),
                "tenant_id": msg.tenant_id,  # whose registry (control API: coverage, reindex)
                "pipeline_version": self.settings.pipeline_version,
                "process_id": msg.process_id,
                "source_updated_at": msg.source_updated_at,
                "staging_prefix": prefix,
                "created_at": ts,
                "updated_at": ts,
                "gsi1pk": ArticleStatus.CLAIMED.value,
                "gsi1sk": ts,
                "gsi2pk": msg.process_id,
                "gsi2sk": keys.gsi2_sk(ArticleStatus.CLAIMED, msg.article_id),
                **self._lease_values(run["run_id"], now),
            }
        )
        codes = self._transact(
            [
                {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": latest,
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": {**run, **_claim_record(result), "claim": "NEW"},
                    }
                },
            ]
        )
        if codes is not None:
            # Another run created LATEST first; decide again against what it wrote.
            return self.claim(msg, run["run_id"], run.get("execution_arn"), run.get("slot"))
        return result

    def _claim_unchanged(self, msg: IngestMessage, run: Item, latest: Item, ts: str) -> ClaimResult:
        counter = "failed" if latest["status"] == ArticleStatus.FAILED_PERMANENT else "skipped"
        result = ClaimResult("SKIP", ClaimKind.UNCHANGED, int(latest["doc_version"]))
        run_item = {
            **run,
            **_claim_record(result),
            "claim": ClaimKind.UNCHANGED.value,
            "outcome": RunOutcome.SKIPPED_UNCHANGED.value,
            "ended_at": ts,
        }
        self._with_rollup(
            [{"Put": {"TableName": self.table.name, "Item": run_item}}],
            process_id=msg.process_id,
            article_id=msg.article_id,
            counter=counter,
            run_id=run["run_id"],
            ts=ts,
            latest_index=None,
            first_only=True,
        )
        self._advance_source_time(msg)
        return result

    def _advance_source_time(self, msg: IngestMessage) -> None:
        """Keep LATEST's source_updated_at the newest seen, so later ordering checks hold."""
        if not msg.source_updated_at:
            return
        try:
            self.table.update_item(
                Key=keys.latest_key(msg.article_id),
                UpdateExpression="SET source_updated_at = :src",
                ConditionExpression=(
                    "attribute_exists(pk) AND (attribute_not_exists(source_updated_at)"
                    " OR source_updated_at < :src)"
                ),
                ExpressionAttributeValues={":src": msg.source_updated_at},
            )
        except ClientError as err:
            if not _is_condition_failure(err):
                raise

    def _claim_metadata(
        self, msg: IngestMessage, run: Item, latest: Item, now: int, ts: str
    ) -> ClaimResult:
        """Same content and title, other metadata or tenant: the Index step rewrites those on the
        article's rows in every write table and ends the run (counted like an unchanged
        article). The version, staged files and vectors stay."""
        doc_version = int(latest["doc_version"])
        result = ClaimResult(
            "METADATA",
            ClaimKind.METADATA,
            doc_version,
            StepName.INDEX,
            str(latest.get("staging_prefix") or staging_prefix(msg.article_id, doc_version)),
            None,
            self._write_tables(msg),
        )
        sets, values = self._status_sets(ArticleStatus.CLAIMED, msg.article_id, ts)
        lease = self._lease_values(run["run_id"], now)
        sets += [
            "#stage = :stage",
            "run_id = :run",
            "meta_sha256 = :meta",
            "lease_owner = :run",
            "lease_expires_at = :exp",
            "gsi3pk = :lease",
            "gsi3sk = :exp",
        ]
        values |= {
            ":stage": StepName.VALIDATE.value,  # it runs as the Index step
            ":run": run["run_id"],
            ":meta": msg.meta_sha256,
            ":exp": lease["lease_expires_at"],
            ":lease": keys.LEASE_BUCKET,
            ":seen": latest.get("run_id"),
            ":now": now,
            ":v": doc_version,
            ":sha": msg.content_sha256,
            ":indexed": ArticleStatus.INDEXED.value,
        }
        condition = (
            "run_id = :seen AND #status = :indexed AND doc_version = :v AND content_sha256 = :sha"
            " AND attribute_not_exists(delete_requested_at) AND (attribute_not_exists("
            "lease_expires_at) OR lease_expires_at <= :now OR lease_owner = :run)"
        )
        if msg.source_updated_at:
            sets.append("source_updated_at = :src")
            values[":src"] = msg.source_updated_at
            condition += (
                " AND (attribute_not_exists(source_updated_at) OR source_updated_at <= :src)"
            )
        removes = _set_tenant(msg, sets, values)
        update = {
            "Update": {
                "TableName": self.table.name,
                "Key": keys.latest_key(msg.article_id),
                "UpdateExpression": "SET " + ", ".join(sets) + removes,
                "ConditionExpression": condition,
                "ExpressionAttributeNames": {"#status": "status", "#stage": "stage"},
                "ExpressionAttributeValues": values,
            }
        }
        put_run = {
            "Put": {
                "TableName": self.table.name,
                "Item": {**run, **_claim_record(result), "claim": ClaimKind.METADATA.value},
            }
        }
        if self._transact([update, put_run]) is not None:
            self._put_run(run, ClaimKind.METADATA, RunOutcome.STALE, ts)
            return ClaimResult("STALE")
        return result

    def _claim_changed(
        self, msg: IngestMessage, run: Item, latest: Item, now: int, ts: str
    ) -> ClaimResult:
        prior_version = latest.get("doc_version")
        same_content = latest.get("content_sha256") == msg.content_sha256 and not _title_changed(
            msg, latest
        )
        if (
            msg.from_stage is not None
            and prior_version is not None
            and same_content
            and latest.get("status") != ArticleStatus.DELETED
        ):
            # Replay from a step: reuse that version's staged inputs. Only for the same content:
            # otherwise the staged output is the old content's, and it would be indexed under
            # the new checksum.
            doc_version, start = int(prior_version), msg.from_stage
        else:
            doc_version, start = int(prior_version or 0) + 1, StepName.FETCH
        claim = ClaimKind.UNCHANGED if same_content else ClaimKind.CHANGED
        prefix = staging_prefix(msg.article_id, doc_version)
        result = ClaimResult(
            "RUN", claim, doc_version, start, prefix, None, self._write_tables(msg)
        )
        stage = start.previous or StepName.CLAIM
        sets, values = self._status_sets(ArticleStatus.CLAIMED, msg.article_id, ts)
        lease = self._lease_values(run["run_id"], now)
        sets += [
            "#stage = :stage",
            "run_id = :run",
            "doc_version = :v",
            "content_sha256 = :sha",
            "pipeline_version = :pv",
            "staging_prefix = :prefix",
            "lease_owner = :run",
            "lease_expires_at = :exp",
            "gsi3pk = :lease",
            "gsi3sk = :exp",
        ]
        values |= {
            ":stage": stage.value,
            ":run": run["run_id"],
            ":v": doc_version,
            ":sha": msg.content_sha256,
            ":pv": self.settings.pipeline_version,
            ":prefix": prefix,
            ":exp": lease["lease_expires_at"],
            ":lease": keys.LEASE_BUCKET,
            ":seen": latest.get("run_id"),
            ":now": now,
        }
        if msg.process_id:
            sets += ["process_id = :pid", "gsi2pk = :pid"]
            values[":pid"] = msg.process_id
        for i, (name, value) in enumerate(sorted(_fingerprints(msg).items())):
            sets.append(f"{name} = :fp{i}")
            values[f":fp{i}"] = value
        condition = (
            "run_id = :seen AND (attribute_not_exists(lease_expires_at)"
            " OR lease_expires_at <= :now OR lease_owner = :run)"
        )
        if msg.source_updated_at:
            sets.append("source_updated_at = :src")
            values[":src"] = msg.source_updated_at
            # Never let an older producer update replace a newer one.
            condition += (
                " AND (attribute_not_exists(source_updated_at) OR source_updated_at <= :src)"
            )
        tenant_removed = _set_tenant(msg, sets, values).removeprefix(" REMOVE ")
        update = {
            "Update": {
                "TableName": self.table.name,
                "Key": keys.latest_key(msg.article_id),
                "UpdateExpression": "SET "
                + ", ".join(sets)
                # A newer update than a pending delete wins: the article lives on.
                + " REMOVE error_class, error_code, error_msg, next_retry_at,"
                " delete_requested_at, delete_run_id, gsi4pk, gsi4sk"
                + (f", {tenant_removed}" if tenant_removed else ""),
                "ConditionExpression": condition,
                "ExpressionAttributeNames": {"#status": "status", "#stage": "stage"},
                "ExpressionAttributeValues": values,
            }
        }
        put_run = {
            "Put": {
                "TableName": self.table.name,
                "Item": {**run, **_claim_record(result), "claim": claim.value},
            }
        }
        if self._transact([update, put_run]) is not None:
            self._put_run(run, claim, RunOutcome.STALE, ts)
            return ClaimResult("STALE")
        return result

    def _claim_delete(
        self, msg: IngestMessage, run: Item, latest: Item | None, now: int, ts: str
    ) -> ClaimResult:
        """A REMOVE is never dropped. If a run holds the article, a marker on LATEST makes that
        run hand the article over at its next step; the reconciler sweeps markers left behind.
        force (orphan repair) deletes even what LATEST says is gone or never had."""
        if latest is None and msg.force:
            return self._claim_delete_orphan(msg, run, now, ts)
        if latest is None or (latest.get("status") == ArticleStatus.DELETED and not msg.force):
            self._put_run(run, ClaimKind.DELETE, RunOutcome.DELETED, ts)
            return ClaimResult("DONE", ClaimKind.DELETE)
        if not self._lease_free(latest, run["run_id"], now):
            marked = self.request_delete(msg, run["run_id"], ts)
            if marked is None or not self._lease_free(marked, run["run_id"], now):
                self._put_run(run, ClaimKind.DELETE, RunOutcome.DELETE_DEFERRED, ts)
                return ClaimResult("DONE", ClaimKind.DELETE)
            latest = marked  # the run ended meanwhile: take the article now
        result = ClaimResult(
            "DELETE",
            ClaimKind.DELETE,
            int(latest.get("doc_version") or 0),
            StepName.INDEX,
            latest.get("staging_prefix"),
        )
        lease = self._lease_values(run["run_id"], now)
        values: dict[str, Any] = {
            ":run": run["run_id"],
            ":stage": StepName.VALIDATE.value,  # the delete runs as the Index step
            ":ts": ts,
            ":exp": lease["lease_expires_at"],
            ":lease": keys.LEASE_BUCKET,
            ":seen": latest.get("run_id"),
            ":now": now,
        }
        sets = (
            "SET run_id = :run, #stage = :stage, updated_at = :ts, lease_owner = :run,"
            " lease_expires_at = :exp, gsi3pk = :lease, gsi3sk = :exp"
        )
        # The tombstone's time: an update no newer than this can't bring the article back.
        tombstone = max(
            filter(None, (msg.source_updated_at, latest.get("source_updated_at"))), default=None
        )
        if tombstone:
            sets += ", source_updated_at = :src"
            values[":src"] = tombstone
        update = {
            "Update": {
                "TableName": self.table.name,
                "Key": keys.latest_key(msg.article_id),
                "UpdateExpression": sets
                + " REMOVE delete_requested_at, delete_run_id, gsi4pk, gsi4sk",
                "ConditionExpression": (
                    "run_id = :seen AND (attribute_not_exists(lease_expires_at)"
                    " OR lease_expires_at <= :now OR lease_owner = :run)"
                ),
                "ExpressionAttributeNames": {"#stage": "stage"},
                "ExpressionAttributeValues": values,
            }
        }
        put_run = {
            "Put": {
                "TableName": self.table.name,
                "Item": {**run, **_claim_record(result), "claim": "DELETE"},
            }
        }
        if self._transact([update, put_run]) is not None:
            # Someone claimed it between our read and write: mark it for them to hand over.
            self.request_delete(msg, run["run_id"], ts)
            self._put_run(run, ClaimKind.DELETE, RunOutcome.DELETE_DEFERRED, ts)
            return ClaimResult("DONE", ClaimKind.DELETE)
        return result

    def _claim_delete_orphan(self, msg: IngestMessage, run: Item, now: int, ts: str) -> ClaimResult:
        """Rows in a vector table with no LATEST at all: a placeholder LATEST holds the lease
        while the Index step deletes them."""
        result = ClaimResult("DELETE", ClaimKind.DELETE, 0, StepName.INDEX, None)
        placeholder = {
            **keys.latest_key(msg.article_id),
            "article_id": msg.article_id,
            "status": ArticleStatus.CLAIMED.value,
            "stage": StepName.VALIDATE.value,
            "run_id": run["run_id"],
            "doc_version": 0,
            "created_at": ts,
            "updated_at": ts,
            "gsi1pk": ArticleStatus.CLAIMED.value,
            "gsi1sk": ts,
            **self._lease_values(run["run_id"], now),
        }
        codes = self._transact(
            [
                {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": placeholder,
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": {**run, **_claim_record(result), "claim": "DELETE"},
                    }
                },
            ]
        )
        if codes is not None:
            return self.claim(msg, run["run_id"], run.get("execution_arn"), run.get("slot"))
        return result

    def request_delete(self, msg: IngestMessage, run_id: str, ts: str) -> Item | None:
        """Mark LATEST: a delete is waiting for the running run. Returns the marked item, or
        None if the article is already deleted."""
        values: dict[str, Any] = {
            ":ts": ts,
            ":run": run_id,
            ":pending": keys.DELETE_PENDING,
            ":deleted": ArticleStatus.DELETED.value,
        }
        try:
            resp = self.table.update_item(
                Key=keys.latest_key(msg.article_id),
                UpdateExpression=(
                    "SET delete_requested_at = :ts, delete_run_id = :run,"
                    " gsi4pk = :pending, gsi4sk = :ts"
                ),
                ConditionExpression="attribute_exists(pk) AND #status <> :deleted",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as err:
            if _is_condition_failure(err):
                return None
            raise
        self._advance_source_time(msg)
        return cast(Item, plain(resp.get("Attributes") or {}))

    def pending_deletes(self, limit: int | None = None) -> list[Item]:
        """LATEST items with a delete waiting for their run to hand over (gsi4)."""
        return self._query(
            limit=limit,
            IndexName="gsi4-stepfail",
            KeyConditionExpression="gsi4pk = :p",
            ExpressionAttributeValues={":p": keys.DELETE_PENDING},
        )

    def yield_to_delete(self, ref: StepRef) -> None:
        """This run found a pending delete: release the article (run outcome PREEMPTED) so the
        delete can claim it."""
        ts = iso_now()
        codes = self._transact(
            [
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": keys.latest_key(ref.article_id),
                        "UpdateExpression": (
                            "SET updated_at = :ts REMOVE lease_owner, lease_expires_at, gsi3pk,"
                            " gsi3sk"
                        ),
                        "ConditionExpression": "run_id = :run AND lease_owner = :run",
                        "ExpressionAttributeValues": {":ts": ts, ":run": ref.run_id},
                    }
                },
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": keys.run_key(ref.article_id, ref.run_id),
                        "UpdateExpression": "SET #outcome = :o, ended_at = :ts",
                        "ExpressionAttributeNames": {"#outcome": "outcome"},
                        "ExpressionAttributeValues": {
                            ":o": RunOutcome.PREEMPTED.value,
                            ":ts": ts,
                        },
                    }
                },
            ]
        )
        if codes is not None:
            raise IngestStale(f"{ref.article_id}: run {ref.run_id} no longer owns the article")
        self._release(ref)

    def _finish_shadow(self, ref: StepRef, status: ArticleStatus, outcome: RunOutcome) -> None:
        """End a shadow run: its SHADOW item and RUN#, and on success LATEST's version marker
        for the table. LATEST's status and the process roll-up are the live run's."""
        if ref.lane is None:
            raise ValueError("not a shadow run")
        ts = iso_now()
        codes = self._transact(
            [
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": self._head_key(ref),
                        "UpdateExpression": (
                            "SET #status = :st, updated_at = :ts REMOVE lease_owner,"
                            " lease_expires_at"
                        ),
                        "ConditionExpression": "run_id = :run AND lease_owner = :run",
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            ":st": status.value,
                            ":ts": ts,
                            ":run": ref.run_id,
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": keys.run_key(ref.article_id, ref.run_id),
                        "UpdateExpression": "SET #outcome = :o, ended_at = :ts",
                        "ExpressionAttributeNames": {"#outcome": "outcome"},
                        "ExpressionAttributeValues": {":o": outcome.value, ":ts": ts},
                    }
                },
            ]
        )
        if codes is not None:
            raise IngestStale(f"{ref.article_id}: shadow run {ref.run_id} lost its lease")
        self._release(ref)
        if status is ArticleStatus.INDEXED and ref.doc_version is not None:
            self.record_table_version(ref.article_id, ref.lane, ref.doc_version)

    def _lost(self, ref: StepRef, what: str) -> IngestStale:
        """Why a run's conditional write failed: a pending delete it must hand over, or another
        run owns the article. A shadow run never hands over; the live run deals with deletes."""
        if ref.lane:
            return IngestStale(f"{ref.article_id}: shadow run {ref.run_id} stopped {what}")
        latest = self.latest(ref.article_id) or {}
        if (
            latest.get("run_id") == ref.run_id
            and latest.get("lease_owner") == ref.run_id
            and "delete_requested_at" in latest
        ):
            return DeletePending(f"{ref.article_id}: deleted while run {ref.run_id} {what}")
        return IngestStale(f"{ref.article_id}: run {ref.run_id} lost its lease {what}")

    # -- step protocol (brief §6.3) ------------------------------------------

    def start_step(
        self,
        ref: StepRef,
        step: StepName,
        input_uri: str | None,
        *,
        count_attempt: bool = True,
    ) -> None:
        """Step 1: move LATEST to this step and mark the step item STARTED (attempt + 1).

        count_attempt=False reopens a step that another state continues (Extract -> OCR) without
        counting it as a retry.

        Raises IngestStale if this run no longer owns the article.
        """
        now, ts = now_epoch(), iso_now()
        status = STEP_STATUS[step]
        expires = now + self.settings.lease_seconds
        if ref.lane:
            sets = [
                "#status = :st",
                "updated_at = :ts",
                "#stage = :step",
                "lease_expires_at = :exp",
            ]
            values: dict[str, Any] = {":st": status.value, ":ts": ts}
        else:
            sets, values = self._status_sets(status, ref.article_id, ts)
            sets += ["#stage = :step", "lease_expires_at = :exp", "gsi3sk = :exp"]
        previous = step.previous or StepName.CLAIM
        values |= {
            ":step": step.value,
            ":prev": previous.value,
            ":exp": expires,
            ":run": ref.run_id,
        }
        latest_update = {
            "Update": {
                "TableName": self.table.name,
                "Key": self._head_key(ref),
                "UpdateExpression": "SET " + ", ".join(sets),
                # The lease owner, not the clock, decides: a run past its lease may go on until
                # another run takes the article over (which replaces lease_owner).
                "ConditionExpression": (
                    "run_id = :run AND lease_owner = :run AND #stage IN (:prev, :step)"
                    " AND attribute_not_exists(delete_requested_at)"
                ),
                "ExpressionAttributeNames": {"#status": "status", "#stage": "stage"},
                "ExpressionAttributeValues": values,
            }
        }
        step_values: dict[str, Any] = {
            ":started": StepStatus.STARTED.value,
            ":ts": ts,
            ":run": ref.run_id,
            ":aid": ref.article_id,
            ":name": step.value,
            ":one": 1,
            ":exp": self._step_ttl(now),
        }
        step_sets = [
            "step_status = :started",
            "started_at = :ts",
            "run_id = :run",
            "article_id = :aid",
            "#step = :name",
            "expires_at_epoch = :exp",
        ]
        if input_uri:
            step_sets.append("input_uri = :in")
            step_values[":in"] = input_uri
        step_update = {
            "Update": {
                "TableName": self.table.name,
                "Key": keys.step_key(ref.article_id, ref.run_id, step),
                "UpdateExpression": "SET "
                + ", ".join(step_sets)
                + (" ADD #attempt :one" if count_attempt else "")
                + " REMOVE ended_at, error_class, error_code, error_msg, gsi4pk, gsi4sk",
                "ExpressionAttributeNames": {"#step": "step"}
                | ({"#attempt": "attempt"} if count_attempt else {}),
                "ExpressionAttributeValues": step_values
                if count_attempt
                else {k: v for k, v in step_values.items() if k != ":one"},
            }
        }
        items = [latest_update, step_update]
        if ref.lane and ref.doc_version is not None:
            items.append(self._latest_is_version(ref.article_id, ref.doc_version))
        codes = self._transact(items)
        if codes is not None:
            raise self._lost(ref, f"before {step}")
        self.extend_slot(ref)

    def annotate(self, ref: StepRef, attrs: Mapping[str, Any]) -> None:
        """Record facts about the run's version on LATEST (e.g. its PII finding), only while this
        run holds the article. Raises IngestStale otherwise."""
        names = {f"#a{i}": k for i, k in enumerate(attrs)}
        values: dict[str, Any] = {f":a{i}": v for i, v in enumerate(attrs.values())}
        try:
            self.table.update_item(
                Key=self._head_key(ref),
                UpdateExpression="SET " + ", ".join(f"#a{i} = :a{i}" for i in range(len(attrs))),
                ConditionExpression="run_id = :run AND lease_owner = :run",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values | {":run": ref.run_id},
            )
        except ClientError as err:
            if _is_condition_failure(err):
                raise IngestStale(f"{ref.article_id}: run {ref.run_id} lost its lease") from err
            raise

    def touch_lease(self, ref: StepRef, step: StepName) -> None:
        """Extend the lease of a run that is still in `step` (long waits such as OCR).

        Raises IngestStale if the run no longer owns the article or moved on.
        """
        now = now_epoch()
        expires = now + self.settings.lease_seconds
        try:
            self.table.update_item(
                Key=self._head_key(ref),
                UpdateExpression="SET lease_expires_at = :exp, updated_at = :ts"
                + ("" if ref.lane else ", gsi3sk = :exp"),
                # A pending delete stops a long step at its next heartbeat.
                ConditionExpression=(
                    "run_id = :run AND lease_owner = :run AND #stage = :step"
                    " AND attribute_not_exists(delete_requested_at)"
                ),
                ExpressionAttributeNames={"#stage": "stage"},
                ExpressionAttributeValues={
                    ":exp": expires,
                    ":ts": iso_now(),
                    ":run": ref.run_id,
                    ":step": step.value,
                },
            )
        except ClientError as err:
            if _is_condition_failure(err):
                raise self._lost(ref, f"during {step}") from err
            raise
        self.extend_slot(ref)

    def succeed_step(
        self,
        ref: StepRef,
        step: StepName,
        duration_ms: int,
        *,
        output_uri: str | None = None,
        output_etag: str | None = None,
        metrics: Mapping[str, Any] | None = None,
        terminal: bool = False,
    ) -> None:
        """Step 3: record success and extend the lease (unless the step ended the run)."""
        now, ts = now_epoch(), iso_now()
        values: dict[str, Any] = {
            ":ok": StepStatus.SUCCEEDED.value,
            ":ts": ts,
            ":ms": duration_ms,
            ":metrics": dict(metrics or {}),
        }
        sets = ["step_status = :ok", "ended_at = :ts", "duration_ms = :ms", "#metrics = :metrics"]
        if output_uri:
            sets.append("output_uri = :out")
            values[":out"] = output_uri
        if output_etag:
            sets.append("output_etag = :etag")
            values[":etag"] = output_etag
        items: list[dict[str, Any]] = [
            {
                "Update": {
                    "TableName": self.table.name,
                    "Key": keys.step_key(ref.article_id, ref.run_id, step),
                    "UpdateExpression": "SET "
                    + ", ".join(sets)
                    + " REMOVE gsi4pk, gsi4sk, error_class, error_code, error_msg",
                    "ExpressionAttributeNames": {"#metrics": "metrics"},
                    "ExpressionAttributeValues": values,
                }
            }
        ]
        if not terminal:
            expires = now + self.settings.lease_seconds
            latest_sets = ["lease_expires_at = :exp"] + ([] if ref.lane else ["gsi3sk = :exp"])
            latest_values: dict[str, Any] = {
                ":exp": expires,
                ":run": ref.run_id,
                ":step": step.value,
            }
            names = {"#stage": "stage"}
            done_status = STEP_SUCCESS_STATUS.get(step)
            if done_status is not None and ref.lane:
                latest_sets.append("#status = :st")
                latest_values[":st"] = done_status.value
                names["#status"] = "status"
            elif done_status is not None:
                status_sets, status_values = self._status_sets(done_status, ref.article_id, ts)
                latest_sets += status_sets
                latest_values |= status_values
                names["#status"] = "status"
            items.append(
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": self._head_key(ref),
                        "UpdateExpression": "SET " + ", ".join(latest_sets),
                        "ConditionExpression": "run_id = :run AND #stage = :step",
                        "ExpressionAttributeNames": names,
                        "ExpressionAttributeValues": latest_values,
                    }
                }
            )
        if self._transact(items) is not None:
            raise IngestStale(f"{ref.article_id}: run {ref.run_id} lost its lease during {step}")

    def fail_step(self, ref: StepRef, step: StepName, duration_ms: int, err: IngestError) -> None:
        """Step 4: mark the step FAILED (last 5 attempts kept) and index it for gsi4-stepfail."""
        ts = iso_now()
        key = keys.step_key(ref.article_id, ref.run_id, step)
        current = self.get(key) or {}
        attempts = list(current.get("attempts") or [])
        attempts.append(
            {"attempt": int(current.get("attempt", 1)), "error_code": err.code, "at": ts}
        )
        self.table.update_item(
            Key=key,
            UpdateExpression=(
                "SET step_status = :failed, ended_at = :ts, duration_ms = :ms, error_class = :cls,"
                " error_code = :code, error_msg = :msg, #attempts = :attempts,"
                " gsi4pk = :g4pk, gsi4sk = :ts"
            ),
            ExpressionAttributeNames={"#attempts": "attempts"},
            ExpressionAttributeValues={
                ":failed": StepStatus.FAILED.value,
                ":ts": ts,
                ":ms": duration_ms,
                ":cls": err.error_class.value,
                ":code": err.code,
                ":msg": err.detail,
                ":attempts": attempts[-MAX_ATTEMPTS_KEPT:],
                ":g4pk": keys.stepfail_pk(step),
            },
        )

    def fail_running_step(self, ref: StepRef, code: str, detail: str) -> None:
        """For failures the step couldn't record itself (Lambda timeout, crash): mark the step
        that LATEST says is running as FAILED, if it's still STARTED."""
        latest = self.latest(ref.article_id)
        if not latest or latest.get("run_id") != ref.run_id:
            return
        try:
            step = StepName(str(latest.get("stage")))
        except ValueError:
            return
        if step is StepName.CLAIM:
            return
        ts = iso_now()
        try:
            self.table.update_item(
                Key=keys.step_key(ref.article_id, ref.run_id, step),
                UpdateExpression=(
                    "SET step_status = :failed, ended_at = :ts, error_class = :cls,"
                    " error_code = :code, error_msg = :msg, gsi4pk = :g4pk, gsi4sk = :ts"
                ),
                ConditionExpression="step_status = :started",
                ExpressionAttributeValues={
                    ":failed": StepStatus.FAILED.value,
                    ":started": StepStatus.STARTED.value,
                    ":ts": ts,
                    ":cls": "permanent",
                    ":code": code,
                    ":msg": detail[:1024],
                    ":g4pk": keys.stepfail_pk(step),
                },
            )
        except ClientError as err:
            if not _is_condition_failure(err):
                raise

    # -- terminal states and roll-up (brief §6.4, correction 2) -------------

    def finish(
        self,
        ref: StepRef,
        status: ArticleStatus,
        outcome: RunOutcome,
        latest_sets: Mapping[str, Any] | None = None,
        *,
        require: ArticleStatus | None = None,
        counter: str | None = None,
    ) -> None:
        """Move LATEST into a terminal status, end the run, release the lease and update the
        process roll-up, all in one transaction.

        The run must still hold the lease, so a late failure can't overwrite a finished run
        (MarkFailed after Finalize). require=<status> instead ends a run that holds no lease
        (the reconciler escalating a FAILED_RETRYABLE article).

        counter: count the run for its process as this instead of the status's counter, and
        only if the article has no outcome there yet (a METADATA run: INDEXED again, counted
        like an unchanged article)."""
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"{status} is not terminal")
        if ref.lane:
            self._finish_shadow(ref, status, outcome)
            return
        ts = iso_now()
        sets, values = self._status_sets(status, ref.article_id, ts)
        names = {"#status": "status"}
        for i, (name, value) in enumerate(sorted((latest_sets or {}).items())):
            names[f"#x{i}"] = name
            values[f":x{i}"] = value
            sets.append(f"#x{i} = :x{i}")
        values[":run"] = ref.run_id
        if require is None:
            condition = "run_id = :run AND lease_owner = :run"
        else:
            condition = "run_id = :run AND #status = :required"
            values[":required"] = require.value
        removes = "lease_owner, lease_expires_at, gsi3pk, gsi3sk"
        if status is ArticleStatus.DELETED:
            removes += ", delete_requested_at, delete_run_id, gsi4pk, gsi4sk"
        else:
            condition += " AND attribute_not_exists(delete_requested_at)"
        if status is not ArticleStatus.FAILED_PERMANENT:
            removes += (
                ", error_class, error_code, error_msg, next_retry_at, retry_count, throttle_count"
            )
        latest_update = {
            "Update": {
                "TableName": self.table.name,
                "Key": keys.latest_key(ref.article_id),
                "UpdateExpression": "SET " + ", ".join(sets) + " REMOVE " + removes,
                "ConditionExpression": condition,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": values,
            }
        }
        run_update = {
            "Update": {
                "TableName": self.table.name,
                "Key": keys.run_key(ref.article_id, ref.run_id),
                "UpdateExpression": "SET #outcome = :outcome, ended_at = :ts",
                "ExpressionAttributeNames": {"#outcome": "outcome"},
                "ExpressionAttributeValues": {":outcome": outcome.value, ":ts": ts},
            }
        }
        try:
            self._with_rollup(
                [latest_update, run_update],
                process_id=ref.process_id,
                article_id=ref.article_id,
                counter=counter or ROLLUP_COUNTER.get(status),
                run_id=ref.run_id,
                ts=ts,
                latest_index=0,
                first_only=counter is not None,
            )
        except IngestStale:
            raise self._lost(ref, f"before finishing {status}") from None
        self._release(ref)

    def mark_retryable(
        self,
        ref: StepRef,
        code: str,
        detail: str,
        next_retry_at: int,
        *,
        counter: Literal["retry_count", "throttle_count"] = "retry_count",
    ) -> None:
        """Transient failure after the state machine's retries: the reconciler re-queues later.
        (A shadow run just ends failed; the drift check re-queues what's still missing.)

        counter: which budget the failure uses up. Throttling (throttle_count) has its own, so a
        busy account doesn't exhaust the retries meant for real failures (retry_count)."""
        if ref.lane:
            self._finish_shadow(ref, ArticleStatus.FAILED_RETRYABLE, RunOutcome.FAILED_RETRYABLE)
            return
        ts = iso_now()
        sets, values = self._status_sets(ArticleStatus.FAILED_RETRYABLE, ref.article_id, ts)
        sets += [
            "error_class = :cls",
            "error_code = :code",
            "error_msg = :msg",
            "next_retry_at = :next",
        ]
        values |= {
            ":cls": "transient",
            ":code": code,
            ":msg": detail[:1024],
            ":next": next_retry_at,
            ":run": ref.run_id,
            ":one": 1,
        }
        codes = self._transact(
            [
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": keys.latest_key(ref.article_id),
                        "UpdateExpression": "SET "
                        + ", ".join(sets)
                        + " ADD #counter :one REMOVE lease_owner, lease_expires_at, gsi3pk,"
                        " gsi3sk",
                        "ConditionExpression": (
                            "run_id = :run AND lease_owner = :run"
                            " AND attribute_not_exists(delete_requested_at)"
                        ),
                        "ExpressionAttributeNames": {"#status": "status", "#counter": counter},
                        "ExpressionAttributeValues": values,
                    }
                },
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": keys.run_key(ref.article_id, ref.run_id),
                        "UpdateExpression": "SET #outcome = :o, ended_at = :ts",
                        "ExpressionAttributeNames": {"#outcome": "outcome"},
                        "ExpressionAttributeValues": {
                            ":o": RunOutcome.FAILED_RETRYABLE.value,
                            ":ts": ts,
                        },
                    }
                },
            ]
        )
        if codes is not None:
            raise self._lost(ref, "before marking it retryable")
        self._release(ref)

    def _with_rollup(
        self,
        items: list[dict[str, Any]],
        *,
        process_id: str | None,
        article_id: str,
        counter: str | None,
        run_id: str,
        ts: str,
        latest_index: int | None,
        first_only: bool = False,
    ) -> None:
        """Commit `items` together with an exact process roll-up.

        One membership item per (process, article) records the article's outcome in that
        process. A first outcome adds the counter; a changed outcome (e.g. failed -> indexed
        after a replay) moves one count from the old counter to the new one; the same outcome
        changes nothing. So indexed + skipped + failed is always the number of distinct articles
        that reached an outcome in the process, which is what the producer's registered_count
        counts.

        first_only: only count an article that has no outcome in the process yet. An unchanged
        re-run (e.g. a metadata PATCH) doesn't change what already happened to it there.
        """
        for _ in range(_ROLLUP_RETRIES):
            rollup = self._rollup_items(
                process_id=process_id,
                article_id=article_id,
                counter=counter,
                run_id=run_id,
                ts=ts,
                first_only=first_only,
            )
            codes = self._transact(items + rollup)
            if codes is None:
                return
            if latest_index is not None and codes[latest_index] == "ConditionalCheckFailed":
                raise IngestStale(f"{article_id}: run {run_id} no longer owns the article")
            # The membership item changed between our read and the transaction: recompute.
        raise make_error(
            ErrorCode.DB_CONFLICT, f"roll-up for {process_id}/{article_id} kept racing"
        )

    def _rollup_items(
        self,
        *,
        process_id: str | None,
        article_id: str,
        counter: str | None,
        run_id: str,
        ts: str,
        first_only: bool = False,
    ) -> list[dict[str, Any]]:
        if not process_id or counter is None:
            return []
        member_key = keys.member_key(process_id, article_id)
        member = self.get(member_key)
        if member is not None and first_only:
            return []
        summary_key = keys.summary_key(process_id)
        if member is None:
            return [
                {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": {
                            **member_key,
                            "kind": "membership",
                            "status": counter,
                            "run_id": run_id,
                            "updated_at": ts,
                        },
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": summary_key,
                        "UpdateExpression": (
                            "ADD #c :one SET updated_at = :ts, gsi1pk = :open, gsi1sk = :ts"
                        ),
                        "ExpressionAttributeNames": {"#c": counter},
                        "ExpressionAttributeValues": {
                            ":one": 1,
                            ":ts": ts,
                            ":open": keys.SUMMARY_OPEN,
                        },
                    }
                },
            ]
        previous = str(member["status"])
        member_update = {
            "Update": {
                "TableName": self.table.name,
                "Key": member_key,
                "UpdateExpression": "SET #s = :new, run_id = :run, updated_at = :ts",
                "ConditionExpression": "#s = :old",
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {
                    ":new": counter,
                    ":old": previous,
                    ":run": run_id,
                    ":ts": ts,
                },
            }
        }
        if previous == counter:
            return [member_update]
        return [
            member_update,
            {
                "Update": {
                    "TableName": self.table.name,
                    "Key": summary_key,
                    "UpdateExpression": (
                        "ADD #old :minus, #new :one"
                        " SET updated_at = :ts, gsi1pk = :open, gsi1sk = :ts"
                    ),
                    "ExpressionAttributeNames": {"#old": previous, "#new": counter},
                    "ExpressionAttributeValues": {
                        ":minus": -1,
                        ":one": 1,
                        ":ts": ts,
                        ":open": keys.SUMMARY_OPEN,
                    },
                }
            },
        ]

    # -- process completion (brief §6.4) -------------------------------------

    def summary(self, process_id: str) -> Item | None:
        return self.get(keys.summary_key(process_id))

    def complete_process_if_done(
        self, process_id: str, producer_process: Item | None
    ) -> str | None:
        """If the producer's process is settled and every registered article has an outcome here,
        mark the SUMMARY COMPLETE / COMPLETE_WITH_ERRORS. A replay that changes the counts later
        (e.g. failed -> indexed) moves it between the two. Returns the new status, or None if
        nothing changed."""
        if not producer_process or not _settled(producer_process):
            return None
        summary = self.summary(process_id) or {}
        counts = {k: int(summary.get(k, 0)) for k in ("indexed", "skipped", "failed")}
        if sum(counts.values()) != int(producer_process.get("registered_count", 0)):
            return None
        status = "COMPLETE_WITH_ERRORS" if counts["failed"] else "COMPLETE"
        if summary.get("status") == status:
            return None
        try:
            self.table.update_item(
                Key=keys.summary_key(process_id),
                UpdateExpression=(
                    "SET #s = :s, completed_at = :ts, registered_count = :reg REMOVE gsi1pk, gsi1sk"
                ),
                ConditionExpression="attribute_not_exists(#s) OR #s <> :s",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s": status,
                    ":ts": iso_now(),
                    ":reg": int(producer_process.get("registered_count", 0)),
                },
            )
        except ClientError as err:
            if _is_condition_failure(err):
                return None
            raise
        return status


def _settled(process: Item) -> bool:
    """The producer's rule (producer.models.is_settled): CLOSED, and every upload has an
    outcome. Processes from before uploads were counted settle on registered + rejected."""
    expected = process.get("expected_count")
    if process.get("status") != "CLOSED" or expected is None:
        return False
    if process.get("tickets_issued") is not None:
        return int(process.get("pending_uploads") or 0) <= 0 and int(
            process["tickets_issued"]
        ) >= int(expected)
    registered = int(process.get("registered_count") or 0)
    return registered + int(process.get("rejected_count") or 0) >= int(expected)
