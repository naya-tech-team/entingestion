"""The status store's table access: reads, paged queries, and transactions that retry
conflicts with concurrent transactions and classify every other cancellation."""

from __future__ import annotations

import random
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from botocore.exceptions import ClientError
from entcommon.dynamo import cancellation_codes, plain

from ingest.config import Settings
from ingest.errors import ErrorCode, IngestError, make_error
from ingest.models.status import (
    ArticleStatus,
)
from ingest.status import keys
from ingest.status.records import Item, StepRef
from ingest.timeutil import days

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table


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


class StoreBase:
    """The table, its client and the settings, and the helpers every part of the store uses."""

    def __init__(self, table: Table, settings: Settings) -> None:
        self.table = table
        self.client = table.meta.client
        self.settings = settings

    def get(self, key: Mapping[str, str]) -> Item | None:
        item = self.table.get_item(Key=dict(key), ConsistentRead=True).get("Item")
        return cast(Item, plain(item)) if item else None

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
                codes = cancellation_codes(err)
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
        return now + days(self.settings.retention.run_ttl_days)

    def _step_ttl(self, now: int) -> int:
        return now + days(self.settings.retention.step_ttl_days)

    def _head_key(self, ref: StepRef) -> dict[str, str]:
        """The item that tracks this run: LATEST, or SHADOW#<table> for a shadow run."""
        if ref.lane:
            return keys.shadow_key(ref.article_id, ref.lane)
        return keys.latest_key(ref.article_id)

    def _lease_values(self, run_id: str, now: int) -> dict[str, Any]:
        expires = now + self.settings.lease_seconds
        return {
            "lease_owner": run_id,
            "lease_expires_at": expires,
            "gsi3pk": keys.LEASE_BUCKET,
            "gsi3sk": expires,
        }

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
