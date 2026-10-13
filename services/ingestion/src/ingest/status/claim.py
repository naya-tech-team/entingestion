"""Claim: decide what a message's run does (run, skip, delete, metadata only, stale) and take the
article's lease. Every write that moves an article between states is conditional on
LATEST.run_id, so a run that lost its lease can't overwrite the run that owns the article."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from botocore.exceptions import ClientError
from entcommon.dynamo import is_condition_failure, plain
from entcommon.timeutil import iso_now

from ingest.errors import IngestStale
from ingest.ids import staging_prefix
from ingest.models.messages import IngestMessage
from ingest.models.status import (
    ArticleStatus,
    ClaimKind,
    RunOutcome,
    StepName,
)
from ingest.status import keys
from ingest.status.claim_rules import _SETTLED_STATUSES, _older_than_latest, _title_changed
from ingest.status.records import ClaimResult, Item, StepRef
from ingest.status.rollup import RollupMixin
from ingest.timeutil import now_epoch
from ingest.vectorstore import registry
from ingest.vectorstore.targets import TARGETS

if TYPE_CHECKING:
    pass


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


def _drop_none(item: Mapping[str, Any]) -> Item:
    return {k: v for k, v in item.items() if v is not None}


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


class ClaimMixin(RollupMixin):
    """Claim, deletes waiting for a running run, and table-version markers."""

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
        must be made for those tables (a re-run), not only its metadata rewritten. A LATEST
        without a tenant_id had no tenant."""
        if latest.get("status") != ArticleStatus.INDEXED:
            return False
        before, after = registry.scope(latest.get("tenant_id")), registry.scope(msg.tenant_id)
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
            if not is_condition_failure(err):
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
            if not is_condition_failure(err):
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
            if is_condition_failure(err):
                return None
            raise
        self._advance_source_time(msg)
        return cast(Item, plain(resp.get("Attributes") or {}))

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
