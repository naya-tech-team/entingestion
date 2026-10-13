"""Read-only access to the producer's tables: never written by this pipeline."""

from __future__ import annotations

from typing import Any, cast

from ingest import aws
from ingest.config import get_settings
from ingest.ids import meta_sha256, title_sha256
from ingest.status.store import plain


def article(article_id: str) -> dict[str, Any] | None:
    settings = get_settings()
    item = (
        aws.table(settings.producer_articles_table)
        .get_item(Key={"article_id": article_id})
        .get("Item")
    )
    return cast(dict[str, Any], plain(item)) if item else None


def fingerprints(row: dict[str, Any]) -> dict[str, str]:
    """IngestMessage.meta_sha256 / title_sha256 of an articles row (stream image or GetItem)."""
    title, metadata = plain(row.get("title")), plain(row.get("metadata"))
    return {
        "meta_sha256": meta_sha256(title, metadata, row.get("tenant_id")),
        "title_sha256": title_sha256(title),
    }


def process(process_id: str) -> dict[str, Any] | None:
    settings = get_settings()
    item = (
        aws.table(settings.producer_process_table)
        .get_item(Key={"process_id": process_id})
        .get("Item")
    )
    return cast(dict[str, Any], plain(item)) if item else None


# The producer spreads a day's writes over this many gsi_updated_shard partitions
# (producer.repository.UPDATED_SHARDS; a day on one partition throttled bulk loads).
UPDATED_SHARDS = 16


def updated_on(day: str) -> list[dict[str, Any]]:
    """Articles the producer updated on a day (YYYY-MM-DD): every shard of gsi_updated_shard."""
    seen: dict[str, dict[str, Any]] = {}
    for shard in range(UPDATED_SHARDS):
        for item in _query("gsi_updated_shard", "updated_shard", f"{day}#{shard:02d}"):
            seen[str(item["article_id"])] = item
    return list(seen.values())


def _query(index: str, key: str, value: str) -> list[dict[str, Any]]:
    table = aws.table(get_settings().producer_articles_table)
    kwargs: dict[str, Any] = {
        "IndexName": index,
        "KeyConditionExpression": f"{key} = :v",
        "ExpressionAttributeValues": {":v": value},
    }
    items: list[dict[str, Any]] = []
    while True:
        resp = table.query(**kwargs)
        items += [cast(dict[str, Any], plain(i)) for i in resp.get("Items", [])]
        start = resp.get("LastEvaluatedKey")
        if not start:
            return items
        kwargs["ExclusiveStartKey"] = start
