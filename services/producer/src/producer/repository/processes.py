"""The process table: one row per batch, with the counters uploads settle into."""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError
from entcommon.dynamo import is_condition_failure

from producer.models import ProcessStatus
from producer.repository.base import Tables, as_item
from producer.repository.keys import GSI_SOURCE, Item


class ProcessRepo(Tables):
    def put_process(self, item: Item) -> None:
        self.process.put_item(Item=item, ConditionExpression="attribute_not_exists(process_id)")

    def get_process(self, process_id: str) -> Item | None:
        resp = self.process.get_item(Key={"process_id": process_id}, ConsistentRead=True)
        found = resp.get("Item")
        return as_item(found) if found else None

    def close_process(self, process_id: str, expected_count: int, now: str) -> Item | None:
        """OPEN -> CLOSED. Returns the new item, or None if the process isn't OPEN (or missing)."""
        try:
            resp = self.process.update_item(
                Key={"process_id": process_id},
                UpdateExpression=(
                    "SET #status = :closed, closed_at = :now, updated_at = :now, "
                    "#expected = :expected"
                ),
                ConditionExpression="#status = :open",
                ExpressionAttributeNames={"#status": "status", "#expected": "expected_count"},
                ExpressionAttributeValues={
                    ":open": ProcessStatus.OPEN.value,
                    ":closed": ProcessStatus.CLOSED.value,
                    ":now": now,
                    ":expected": expected_count,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as err:
            if is_condition_failure(err):
                return None
            raise
        return as_item(resp["Attributes"])

    def list_processes_by_source(
        self, source: str, limit: int, start_key: dict[str, Any] | None
    ) -> tuple[list[Item], dict[str, Any] | None]:
        """A source's processes, newest first (gsi_source)."""
        kwargs: dict[str, Any] = {
            "IndexName": GSI_SOURCE,
            "KeyConditionExpression": "#source = :source",
            "ExpressionAttributeNames": {"#source": "source"},
            "ExpressionAttributeValues": {":source": source},
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        resp = self.process.query(**kwargs)
        items = [as_item(i) for i in resp.get("Items", [])]
        return items, resp.get("LastEvaluatedKey")

    def open_processes_idle_since(self, cutoff: str) -> list[Item]:
        """OPEN processes not updated since `cutoff` (the janitor closes them). A scan: the
        process table holds one small item per batch."""
        items: list[Item] = []
        kwargs: dict[str, Any] = {
            "FilterExpression": "#status = :open AND updated_at < :cutoff",
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {":open": ProcessStatus.OPEN.value, ":cutoff": cutoff},
        }
        while True:
            resp = self.process.scan(**kwargs)
            items += [as_item(i) for i in resp.get("Items", [])]
            if "LastEvaluatedKey" not in resp:
                return items
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
