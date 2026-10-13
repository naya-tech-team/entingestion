"""The producer_uploads table: one row per ticket, and the outcome it settles into its process.

The same table holds synthetic rows keyed by prefix (keys.py): an article's ticket counter
(SEQ#), its process membership markers (MEMBER#) and its delete tombstone (TOMBSTONE#).
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError
from entcommon.dynamo import is_condition_failure

from producer.models import ProcessStatus, UploadStatus
from producer.repository.base import Tables, as_item
from producer.repository.keys import (
    GSI_PENDING,
    GSI_PROCESS_UPLOADS,
    PENDING,
    Item,
    membership_id,
    sequence_id,
)
from producer.repository.transactions import condition_failed, transact_with_retry


class UploadRepo(Tables):
    def next_upload_sequence(self, article_id: str) -> int:
        """The article's next ticket number: orders its uploads by when they were requested,
        which ULIDs from different API instances in the same millisecond don't."""
        resp = self.uploads.update_item(
            Key={"upload_id": sequence_id(article_id)},
            UpdateExpression="SET kind = :kind ADD seq :one",
            ExpressionAttributeValues={":kind": "sequence", ":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"]["seq"])  # type: ignore[arg-type]

    def put_pending_upload(self, item: Item) -> bool:
        """The new PENDING upload, counted in its process (tickets_issued, pending_uploads), in
        one transaction. Returns False if the process isn't OPEN (or is missing)."""
        if item.get("status") != UploadStatus.PENDING:
            raise ValueError("new uploads must be PENDING")
        try:
            transact_with_retry(
                self.client,
                [
                    {
                        "Put": {
                            "TableName": self.uploads.name,
                            "Item": item,
                            "ConditionExpression": "attribute_not_exists(upload_id)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.process.name,
                            "Key": {"process_id": item["process_id"]},
                            "UpdateExpression": (
                                "ADD tickets_issued :one, pending_uploads :one"
                                " SET updated_at = :now"
                            ),
                            "ConditionExpression": "#status = :open",
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": {
                                ":one": 1,
                                ":now": item["created_at"],
                                ":open": ProcessStatus.OPEN.value,
                            },
                        }
                    },
                ],
            )
        except ClientError as err:
            if condition_failed(err):
                return False
            raise
        return True

    def get_upload(self, upload_id: str) -> Item | None:
        resp = self.uploads.get_item(Key={"upload_id": upload_id}, ConsistentRead=True)
        found = resp.get("Item")
        return as_item(found) if found else None

    def list_uploads_by_process(
        self, process_id: str, limit: int, start_key: dict[str, Any] | None
    ) -> tuple[list[Item], dict[str, Any] | None]:
        """A process's uploads, oldest first (gsi_process_uploads). Membership markers carry
        process_id and created_at too, so they're in the index; the filter drops them. Each
        query reads at most the items still missing, so its last evaluated key is always a
        place to resume from."""
        items: list[Item] = []
        kwargs: dict[str, Any] = {
            "IndexName": GSI_PROCESS_UPLOADS,
            "KeyConditionExpression": "process_id = :pid",
            "FilterExpression": "attribute_not_exists(kind)",
            "ExpressionAttributeValues": {":pid": process_id},
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        while True:
            resp = self.uploads.query(Limit=limit - len(items), **kwargs)
            items += [as_item(i) for i in resp.get("Items", [])]
            last_key = resp.get("LastEvaluatedKey")
            if not last_key or len(items) >= limit:
                return items, last_key
            kwargs["ExclusiveStartKey"] = last_key

    # -- janitor --------------------------------------------------------------

    def pending_uploads_before(self, epoch: int, limit: int) -> list[Item]:
        """Uploads still without an outcome whose ticket expired before `epoch` (gsi_pending)."""
        resp = self.uploads.query(
            IndexName=GSI_PENDING,
            KeyConditionExpression="pending = :p AND ticket_expires_epoch < :t",
            ExpressionAttributeValues={":p": PENDING, ":t": epoch},
            Limit=limit,
        )
        return [as_item(i) for i in resp.get("Items", [])]

    def mark_resent(self, upload_id: str, now: str) -> bool:
        """Record that the janitor asked the registrar again for a PENDING upload. False if it
        did already, or the upload has an outcome now (so: ask at most once)."""
        try:
            self.uploads.update_item(
                Key={"upload_id": upload_id},
                UpdateExpression="SET resent_at = :now",
                ConditionExpression="attribute_not_exists(resent_at) AND #status = :pending",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":now": now, ":pending": UploadStatus.PENDING.value},
            )
        except ClientError as err:
            if is_condition_failure(err):
                return False
            raise
        return True

    def expire_upload(self, upload: Item, now: str, expires_at_epoch: int) -> bool:
        """PENDING -> EXPIRED (no object ever arrived); the process counts it as settled."""
        return self._finish(
            upload, UploadStatus.EXPIRED, UploadStatus.PENDING, now=now,
            expires_at_epoch=expires_at_epoch, reason="ticket_expired", counter="expired_count",
        )  # fmt: skip

    # -- registrar ------------------------------------------------------------

    def claim_upload(self, upload_id: str, expires_at_epoch: int) -> Item | None:
        """PENDING|REGISTERING -> REGISTERING. Returns the upload, or None if it's already final.

        Pushes the row's expiry out, so a message redriven from the DLQ days later still finds
        its upload."""
        try:
            resp = self.uploads.update_item(
                Key={"upload_id": upload_id},
                UpdateExpression="SET #status = :registering, expires_at_epoch = :exp",
                ConditionExpression=(
                    "attribute_exists(upload_id) AND #status IN (:pending, :registering)"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":pending": UploadStatus.PENDING.value,
                    ":registering": UploadStatus.REGISTERING.value,
                    ":exp": expires_at_epoch,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as err:
            if is_condition_failure(err):
                return None
            raise
        return as_item(resp["Attributes"])

    def reject_upload(self, upload: Item, reason: str, now: str, expires_at_epoch: int) -> bool:
        """Upload -> REJECTED and process rejected_count + 1, atomically.

        Returns False if another delivery already finished this upload.
        """
        return self._finish(
            upload, UploadStatus.REJECTED, UploadStatus.REGISTERING, now=now,
            expires_at_epoch=expires_at_epoch, reason=reason, counter="rejected_count",
        )  # fmt: skip

    def finish_upload(
        self,
        upload: Item,
        status: UploadStatus,
        now: str,
        expires_at_epoch: int,
        reason: str | None = None,
    ) -> bool:
        """REGISTERING -> status. Returns False if another delivery already finished it."""
        counter = "superseded_count" if status == UploadStatus.SUPERSEDED else None
        return self._finish(
            upload, status, UploadStatus.REGISTERING, now=now, expires_at_epoch=expires_at_epoch,
            reason=reason, counter=counter,
        )  # fmt: skip

    def count_membership(self, process_id: str, article_id: str, now: str) -> bool:
        """Count the article in the process exactly once. Returns False if already counted."""
        try:
            transact_with_retry(
                self.client,
                [
                    {
                        "Put": {
                            "TableName": self.uploads.name,
                            "Item": {
                                "upload_id": membership_id(process_id, article_id),
                                "kind": "membership",
                                "process_id": process_id,
                                "article_id": article_id,
                                # gsi_member: a process's articles, stable and in article order
                                "member_process_id": process_id,
                                "member_article_id": article_id,
                                "created_at": now,
                            },
                            "ConditionExpression": "attribute_not_exists(upload_id)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.process.name,
                            "Key": {"process_id": process_id},
                            "UpdateExpression": "ADD registered_count :one SET updated_at = :now",
                            "ConditionExpression": "attribute_exists(process_id)",
                            "ExpressionAttributeValues": {":one": 1, ":now": now},
                        }
                    },
                ],
            )
        except ClientError as err:
            if condition_failed(err, first_only=True):
                return False
            raise
        return True

    def _finish(
        self,
        upload: Item,
        status: UploadStatus,
        current: UploadStatus,
        *,
        now: str,
        expires_at_epoch: int,
        reason: str | None,
        counter: str | None = None,
    ) -> bool:
        """Upload current -> status, and in the same transaction the process's counters: one
        upload fewer pending, plus `counter`. Returns False if the upload isn't in `current` any
        more."""
        names = {"#status": "status"}
        values: dict[str, Any] = {
            ":status": status.value,
            ":current": current.value,
            ":now": now,
            ":exp": expires_at_epoch,
        }
        expression = "SET #status = :status, finished_at = :now, expires_at_epoch = :exp"
        if reason is not None:
            values[":reason"] = reason
            expression += ", reason = :reason"
        items: list[dict[str, Any]] = [
            {
                "Update": {
                    "TableName": self.uploads.name,
                    "Key": {"upload_id": upload["upload_id"]},
                    "UpdateExpression": expression + " REMOVE pending",
                    "ConditionExpression": "#status = :current",
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": values,
                }
            }
        ]
        adds = ["pending_uploads :minus"]
        add_values: dict[str, Any] = {":now": now, ":minus": -1}
        if counter:
            # Only the values the expression uses: DynamoDB rejects unused ones.
            adds.append(f"{counter} :one")
            add_values[":one"] = 1
        items.append(
            {
                "Update": {
                    "TableName": self.process.name,
                    "Key": {"process_id": upload["process_id"]},
                    "UpdateExpression": "ADD " + ", ".join(adds) + " SET updated_at = :now",
                    "ConditionExpression": "attribute_exists(process_id)",
                    "ExpressionAttributeValues": add_values,
                }
            }
        )
        try:
            transact_with_retry(self.client, items)
        except ClientError as err:
            if condition_failed(err, first_only=True):
                return False
            raise
        return True
