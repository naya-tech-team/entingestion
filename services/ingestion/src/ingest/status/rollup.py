"""The exact process roll-up (one membership item per process and article) and process
completion."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError
from entcommon.dynamo import is_condition_failure
from entcommon.timeutil import iso_now

from ingest.errors import ErrorCode, IngestStale, make_error
from ingest.status import keys
from ingest.status.claim_rules import _settled
from ingest.status.records import Item
from ingest.status.slots import SlotsMixin

if TYPE_CHECKING:
    pass


_ROLLUP_RETRIES = 3


class RollupMixin(SlotsMixin):
    """Commit writes together with the process roll-up; complete processes."""

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
            if is_condition_failure(err):
                return None
            raise
        return status
