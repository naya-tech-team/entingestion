"""Reads on ingestion_status: LATEST, runs, items by status, configuration, leases, processes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from entcommon.dynamo import plain
from entcommon.timeutil import iso_now

from ingest.models.status import (
    ArticleStatus,
)
from ingest.status import keys
from ingest.status.base import StoreBase
from ingest.status.records import Item

if TYPE_CHECKING:
    pass


class ReadsMixin(StoreBase):
    """Reads (and the small #CONFIG and SUMMARY writes that go with them)."""

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

    def pending_deletes(self, limit: int | None = None) -> list[Item]:
        """LATEST items with a delete waiting for their run to hand over (gsi4)."""
        return self._query(
            limit=limit,
            IndexName="gsi4-stepfail",
            KeyConditionExpression="gsi4pk = :p",
            ExpressionAttributeValues={":p": keys.DELETE_PENDING},
        )

    def summary(self, process_id: str) -> Item | None:
        return self.get(keys.summary_key(process_id))
