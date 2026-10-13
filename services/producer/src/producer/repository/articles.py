"""The articles table: one row per article, owned by its newest upload; its stream feeds
ingestion."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from botocore.exceptions import ClientError
from entcommon.dynamo import is_condition_failure

from producer.repository.base import Tables, as_item
from producer.repository.keys import GSI_MEMBER, Item, tombstone_id, updated_shard
from producer.repository.transactions import condition_failed, transact_with_retry

BATCH_GET = 100  # BatchGetItem's limit


class ArticleRepo(Tables):
    def get_article(self, article_id: str) -> Item | None:
        resp = self.articles.get_item(Key={"article_id": article_id}, ConsistentRead=True)
        found = resp.get("Item")
        return as_item(found) if found else None

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
        for start in range(0, len(ids), BATCH_GET):
            request: dict[str, Any] = {
                self.articles.name: {
                    "Keys": [{"article_id": a} for a in ids[start : start + BATCH_GET]],
                    "ConsistentRead": True,
                }
            }
            while request:
                resp = self.client.batch_get_item(RequestItems=request)
                for row in resp.get("Responses", {}).get(self.articles.name, []):
                    found[str(row["article_id"])] = as_item(row)
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
        return as_item(resp["Attributes"])

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
            if condition_failed(err):
                return False  # gone, or replaced by a newer upload meanwhile
            raise
        return True

    def apply_article(self, fields: Mapping[str, Any], upload_metadata: Any) -> bool:
        """Upsert the article from a verified upload.

        Writes every field, even if the content is unchanged, so the stream tells the pipeline the
        article joined this process. first_process_id, created_at and (when the upload carries
        none) metadata are only set on first write. Returns False if a newer upload already owns
        the article (out-of-order delivery): the newest ticket wins, by the article's ticket
        sequence (upload_seq).
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
        names["#seq"] = "upload_seq"
        values[":seq"] = fields["upload_seq"]
        condition = "attribute_not_exists(article_id) OR #seq <= :seq"

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
            if condition_failed(err):
                return False
            raise
        return True
