"""DynamoDB access for the process, articles and producer_uploads tables."""

from __future__ import annotations

import base64
import binascii
import json
import random
import time
import zlib
from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

import boto3
from botocore.exceptions import ClientError

from producer.models import ProcessStatus, UploadStatus

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

Item = dict[str, Any]

GSI_PROCESS = "gsi_process"  # legacy listing index (articles move between processes)
GSI_SOURCE = "gsi_source"  # process table: a source's processes by created_at
GSI_MEMBER = "gsi_member"  # uploads table: membership markers by process, article order
GSI_PENDING = "gsi_pending"  # uploads table: tickets without an outcome, by ticket expiry
GSI_PROCESS_UPLOADS = "gsi_process_uploads"  # uploads table: a process's uploads by created_at
PENDING = "P"  # value of the sparse `pending` attribute while an upload has no outcome
UPDATED_SHARDS = 16  # gsi_updated_shard partitions per day


def updated_shard(article_id: str, date: str) -> str:
    """Spread a day's article writes over UPDATED_SHARDS partitions of gsi_updated_shard."""
    return f"{date}#{zlib.crc32(article_id.encode()) % UPDATED_SHARDS:02d}"


def tombstone_id(article_id: str) -> str:
    return f"TOMBSTONE#{article_id}"


def sequence_id(article_id: str) -> str:
    return f"SEQ#{article_id}"


def from_dynamo(value: Any) -> Any:
    """Convert DynamoDB Decimals back to int/float so Pydantic sees plain numbers (brief §6.5)."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, Mapping):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    return value


def is_condition_failure(err: ClientError) -> bool:
    return err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def cancellation_codes(err: ClientError) -> list[str] | None:
    """Per-item codes of a cancelled transaction, or None if err isn't a cancellation."""
    if err.response.get("Error", {}).get("Code") != "TransactionCanceledException":
        return None
    reasons = err.response.get("CancellationReasons") or []
    return [str(r.get("Code", "None")) for r in reasons]


# Registrars counting into the same process row at once cancel each other's transactions with
# TransactionConflict. That's contention, not a failed condition: retry with jitter.
CONFLICT_ATTEMPTS = 6
CONFLICT_BASE_S = 0.05


def transact_with_retry(client: Any, items: list[dict[str, Any]]) -> None:
    """transact_write_items, retrying TransactionConflict. Other errors propagate."""
    for attempt in range(CONFLICT_ATTEMPTS):
        try:
            client.transact_write_items(TransactItems=items)
        except ClientError as err:
            codes = cancellation_codes(err) or []
            if "TransactionConflict" not in codes or "ConditionalCheckFailed" in codes:
                raise
            if attempt == CONFLICT_ATTEMPTS - 1:
                raise
            time.sleep(random.uniform(0, CONFLICT_BASE_S * 2**attempt))  # noqa: S311
        else:
            return


def membership_id(process_id: str, article_id: str) -> str:
    return f"MEMBER#{process_id}#{article_id}"


class InvalidPageToken(ValueError):
    pass


def encode_page_token(last_evaluated_key: Mapping[str, Any] | None) -> str | None:
    if not last_evaluated_key:
        return None
    raw = json.dumps(from_dynamo(last_evaluated_key), separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_page_token(token: str) -> dict[str, Any]:
    try:
        padded = token + "=" * (-len(token) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidPageToken("next_token is not valid") from exc
    if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
        raise InvalidPageToken("next_token is not valid")
    return value


class Repository:
    def __init__(self, process_table: Table, articles_table: Table, uploads_table: Table) -> None:
        self.process = process_table
        self.articles = articles_table
        self.uploads = uploads_table
        # Resource-level client: takes plain Python values (brief §6.5).
        self.client = process_table.meta.client

    @classmethod
    def from_names(cls, process: str, articles: str, uploads: str) -> Repository:
        ddb = boto3.resource("dynamodb")
        return cls(ddb.Table(process), ddb.Table(articles), ddb.Table(uploads))

    # -- process ------------------------------------------------------------

    def put_process(self, item: Item) -> None:
        self.process.put_item(Item=item, ConditionExpression="attribute_not_exists(process_id)")

    def get_process(self, process_id: str) -> Item | None:
        resp = self.process.get_item(Key={"process_id": process_id}, ConsistentRead=True)
        item = resp.get("Item")
        return cast(Item, from_dynamo(item)) if item else None

    def close_process(self, process_id: str, expected_count: int | None, now: str) -> Item | None:
        """OPEN -> CLOSED. Returns the new item, or None if the process isn't OPEN (or missing)."""
        names = {"#status": "status", "#expected": "expected_count"}
        values: dict[str, Any] = {
            ":open": ProcessStatus.OPEN.value,
            ":closed": ProcessStatus.CLOSED.value,
            ":now": now,
        }
        if expected_count is None:
            # Default: the number registered so far. Only names actually used are passed,
            # because DynamoDB rejects unused ExpressionAttributeNames (brief §6.3).
            names["#registered"] = "registered_count"
            values[":zero"] = 0
            expected_expr = "if_not_exists(#registered, :zero)"
        else:
            values[":expected"] = expected_count
            expected_expr = ":expected"
        try:
            resp = self.process.update_item(
                Key={"process_id": process_id},
                UpdateExpression=(
                    "SET #status = :closed, closed_at = :now, updated_at = :now, "
                    f"#expected = {expected_expr}"
                ),
                ConditionExpression="#status = :open",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as err:
            if is_condition_failure(err):
                return None
            raise
        return cast(Item, from_dynamo(resp["Attributes"]))

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
        items = [cast(Item, from_dynamo(i)) for i in resp.get("Items", [])]
        return items, resp.get("LastEvaluatedKey")

    # -- uploads ------------------------------------------------------------

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
            codes = cancellation_codes(err)
            if codes and "ConditionalCheckFailed" in codes:
                return False
            raise
        return True

    def pending_uploads_before(self, epoch: int, limit: int) -> list[Item]:
        """Uploads still without an outcome whose ticket expired before `epoch` (gsi_pending)."""
        resp = self.uploads.query(
            IndexName=GSI_PENDING,
            KeyConditionExpression="pending = :p AND ticket_expires_epoch < :t",
            ExpressionAttributeValues={":p": PENDING, ":t": epoch},
            Limit=limit,
        )
        return [cast(Item, from_dynamo(i)) for i in resp.get("Items", [])]

    def expire_upload(self, upload: Item, now: str, expires_at_epoch: int) -> bool:
        """PENDING -> EXPIRED (no object ever arrived); the process counts it as settled."""
        return self._finish(
            upload, UploadStatus.EXPIRED, UploadStatus.PENDING, now=now,
            expires_at_epoch=expires_at_epoch, reason="ticket_expired", counter="expired_count",
        )  # fmt: skip

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
            items += [cast(Item, from_dynamo(i)) for i in resp.get("Items", [])]
            last_key = resp.get("LastEvaluatedKey")
            if not last_key or len(items) >= limit:
                return items, last_key
            kwargs["ExclusiveStartKey"] = last_key

    def get_upload(self, upload_id: str) -> Item | None:
        resp = self.uploads.get_item(Key={"upload_id": upload_id}, ConsistentRead=True)
        item = resp.get("Item")
        return cast(Item, from_dynamo(item)) if item else None

    # -- articles -----------------------------------------------------------

    def get_article(self, article_id: str) -> Item | None:
        resp = self.articles.get_item(Key={"article_id": article_id}, ConsistentRead=True)
        item = resp.get("Item")
        return cast(Item, from_dynamo(item)) if item else None

    def list_articles_by_process(
        self, process_id: str, limit: int, start_key: dict[str, Any] | None
    ) -> tuple[list[Item], dict[str, Any] | None]:
        """The articles registered in a process, in article_id order, from its membership
        markers (gsi_member): stable while articles change or move on to later processes.
        Articles deleted since are left out."""
        kwargs: dict[str, Any] = {
            "IndexName": GSI_MEMBER,
            "KeyConditionExpression": "member_process_id = :pid",
            "ExpressionAttributeValues": {":pid": process_id},
            "Limit": limit,
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        resp = self.uploads.query(**kwargs)
        ids = [str(i["member_article_id"]) for i in resp.get("Items", [])]
        found = self._batch_get_articles(ids)
        return [found[a] for a in ids if a in found], resp.get("LastEvaluatedKey")

    def _batch_get_articles(self, ids: list[str]) -> dict[str, Item]:
        found: dict[str, Item] = {}
        for start in range(0, len(ids), 100):
            request: dict[str, Any] = {
                self.articles.name: {
                    "Keys": [{"article_id": a} for a in ids[start : start + 100]],
                    "ConsistentRead": True,
                }
            }
            while request:
                resp = self.client.batch_get_item(RequestItems=request)
                for item in resp.get("Responses", {}).get(self.articles.name, []):
                    found[str(item["article_id"])] = cast(Item, from_dynamo(item))
                request = resp.get("UnprocessedKeys") or {}
        return found

    def patch_article(
        self,
        article_id: str,
        fields: Mapping[str, Any],
        now: str,
        today: str,
        *,
        if_unchanged_since: str | None = None,
    ) -> Item | None:
        """Update title/metadata; a None value removes the attribute. With if_unchanged_since,
        only if the article's updated_at is still that value (optimistic concurrency).

        Returns the new item, or None if the article doesn't exist (or changed).
        """
        names = {
            "#updated_at": "updated_at",
            "#updated_date": "updated_date",
            "#updated_shard": "updated_shard",
        }
        values: dict[str, Any] = {
            ":now": now,
            ":today": today,
            ":shard": updated_shard(article_id, today),
        }
        sets = ["#updated_at = :now", "#updated_date = :today", "#updated_shard = :shard"]
        removes: list[str] = []
        for i, (name, value) in enumerate(sorted(fields.items())):
            names[f"#f{i}"] = name
            if value is None:
                removes.append(f"#f{i}")
            else:
                values[f":f{i}"] = value
                sets.append(f"#f{i} = :f{i}")
        expression = "SET " + ", ".join(sets)
        if removes:
            expression += " REMOVE " + ", ".join(removes)
        condition = "attribute_exists(article_id)"
        if if_unchanged_since is not None:
            condition += " AND #updated_at = :seen"
            values[":seen"] = if_unchanged_since
        try:
            resp = self.articles.update_item(
                Key={"article_id": article_id},
                UpdateExpression=expression,
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as err:
            if is_condition_failure(err):
                return None
            raise
        return cast(Item, from_dynamo(resp["Attributes"]))

    def delete_article(self, article: Item, now: str) -> bool:
        """Delete the row (the stream REMOVE takes it out of ingestion) and leave a tombstone
        holding the upload it had, so no event for that upload or an older one revives it."""
        try:
            transact_with_retry(
                self.client,
                [
                    {
                        "Delete": {
                            "TableName": self.articles.name,
                            "Key": {"article_id": article["article_id"]},
                            "ConditionExpression": "#upload_id = :upl",
                            "ExpressionAttributeNames": {"#upload_id": "upload_id"},
                            "ExpressionAttributeValues": {":upl": article["upload_id"]},
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.uploads.name,
                            "Item": {
                                "upload_id": tombstone_id(str(article["article_id"])),
                                "kind": "tombstone",
                                "article_id": article["article_id"],
                                "deleted_upload_id": article["upload_id"],
                                "deleted_at": now,
                            },
                        }
                    },
                ],
            )
        except ClientError as err:
            codes = cancellation_codes(err)
            if codes and "ConditionalCheckFailed" in codes:
                return False  # gone, or replaced by a newer upload meanwhile
            raise
        return True

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
            items += [cast(Item, from_dynamo(i)) for i in resp.get("Items", [])]
            if "LastEvaluatedKey" not in resp:
                return items
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    # -- registrar (brief §6.2) ---------------------------------------------
    # Transactions go through the resource's client, which serializes plain Python values
    # itself; pre-serialized {"S": ...} values would be double-wrapped (brief §6.5).

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
        return cast(Item, from_dynamo(resp["Attributes"]))

    def reject_upload(self, upload: Item, reason: str, now: str, expires_at_epoch: int) -> bool:
        """Upload -> REJECTED and process rejected_count + 1, atomically.

        Returns False if another delivery already finished this upload.
        """
        return self._finish(
            upload, UploadStatus.REJECTED, UploadStatus.REGISTERING, now=now,
            expires_at_epoch=expires_at_epoch, reason=reason, counter="rejected_count",
            always_count=True,
        )  # fmt: skip

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
        always_count: bool = False,
    ) -> bool:
        """Upload current -> status, and in the same transaction the process's counters: the
        upload is no longer pending (if it was counted when its ticket was issued), plus
        `counter`. Returns False if the upload isn't in `current` any more."""
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
        adds: list[str] = []
        add_values: dict[str, Any] = {":now": now}
        if upload.get("counted"):
            adds.append("pending_uploads :minus")
            add_values[":minus"] = -1
        if counter and (always_count or upload.get("counted")):
            adds.append(f"{counter} :one")
            add_values[":one"] = 1
        if adds:
            # Only the values the expression uses: DynamoDB rejects unused ones.
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
            codes = cancellation_codes(err)
            if codes and codes[0] == "ConditionalCheckFailed":
                return False
            raise
        return True

    def apply_article(self, fields: Mapping[str, Any], upload_metadata: Any) -> bool:
        """Upsert the article from a verified upload (brief §6.2 step 5).

        Writes every field, even if the content is unchanged, so the stream tells the pipeline the
        article joined this process. first_process_id, created_at and (when the upload carries
        none) metadata are only set on first write. Returns False if a newer upload already owns
        the article (out-of-order delivery).
        """
        once = {"first_process_id", "created_at"}
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        sets: list[str] = []
        removes: list[str] = []
        for i, (name, value) in enumerate(sorted(fields.items())):
            if name == "article_id":
                continue
            names[f"#a{i}"] = name
            if value is None:
                removes.append(f"#a{i}")
                continue
            values[f":a{i}"] = value
            if name in once:
                sets.append(f"#a{i} = if_not_exists(#a{i}, :a{i})")
            else:
                sets.append(f"#a{i} = :a{i}")
        names["#metadata"] = "metadata"
        if upload_metadata is None:
            values[":empty"] = {}
            sets.append("#metadata = if_not_exists(#metadata, :empty)")
        else:
            values[":metadata"] = upload_metadata
            sets.append("#metadata = :metadata")
        names["#upload_id"] = "upload_id"
        values[":upload_id"] = fields["upload_id"]
        # Newest ticket wins: by the article's ticket sequence when both have one, else by
        # upload_id (ULIDs, for uploads from before sequences).
        condition = "attribute_not_exists(article_id) OR #upload_id <= :upload_id"
        if fields.get("upload_seq") is not None:
            names["#seq"] = "upload_seq"
            condition = (
                "attribute_not_exists(article_id)"
                " OR (attribute_exists(#seq) AND #seq <= :a_seq)"
                " OR (attribute_not_exists(#seq) AND #upload_id <= :upload_id)"
            )
            values[":a_seq"] = fields["upload_seq"]

        expression = "SET " + ", ".join(sets)
        if removes:
            expression += " REMOVE " + ", ".join(removes)
        try:
            transact_with_retry(
                self.client,
                [
                    {
                        "Update": {
                            "TableName": self.articles.name,
                            "Key": {"article_id": fields["article_id"]},
                            "UpdateExpression": expression,
                            "ConditionExpression": condition,
                            "ExpressionAttributeNames": names,
                            "ExpressionAttributeValues": values,
                        }
                    },
                    {
                        # A deleted article stays deleted for uploads from before the delete
                        # (a late or redriven event); a newer upload brings it back.
                        "ConditionCheck": {
                            "TableName": self.uploads.name,
                            "Key": {"upload_id": tombstone_id(fields["article_id"])},
                            "ConditionExpression": (
                                "attribute_not_exists(upload_id) OR deleted_upload_id < :upl"
                            ),
                            "ExpressionAttributeValues": {":upl": fields["upload_id"]},
                        }
                    },
                ],
            )
        except ClientError as err:
            codes = cancellation_codes(err)
            if codes and "ConditionalCheckFailed" in codes:
                return False
            raise
        return True

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
            codes = cancellation_codes(err)
            if codes and codes[0] == "ConditionalCheckFailed":
                return False
            raise
        return True

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
