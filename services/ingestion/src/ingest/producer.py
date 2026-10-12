"""Read-only access to the producer's tables (brief §8.4): never written by this pipeline."""

from __future__ import annotations

from collections.abc import Iterable
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


_BATCH_GET = 100


def tenants(article_ids: Iterable[str]) -> dict[str, str | None]:
    """article_id -> the row's tenant_id (None: no row, or no tenant), in BatchGetItem pages."""
    table_name = get_settings().producer_articles_table
    wanted = list(dict.fromkeys(article_ids))
    found: dict[str, str | None] = dict.fromkeys(wanted)
    for i in range(0, len(wanted), _BATCH_GET):
        request: dict[str, Any] = {
            table_name: {
                "Keys": [{"article_id": a} for a in wanted[i : i + _BATCH_GET]],
                "ProjectionExpression": "article_id, tenant_id",
            }
        }
        while request:
            resp = aws.dynamodb().batch_get_item(RequestItems=request)
            for item in resp.get("Responses", {}).get(table_name, []):
                found[str(item["article_id"])] = str(item.get("tenant_id") or "") or None
            request = dict(resp.get("UnprocessedKeys") or {})
    return found


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
    """Articles the producer updated on a day (YYYY-MM-DD): every shard of gsi_updated_shard,
    plus the older single-partition gsi_updated for rows written before the shards existed."""
    seen: dict[str, dict[str, Any]] = {}
    for shard in range(UPDATED_SHARDS):
        for item in _query("gsi_updated_shard", "updated_shard", f"{day}#{shard:02d}"):
            seen[str(item["article_id"])] = item
    for item in _query("gsi_updated", "updated_date", day):
        seen.setdefault(str(item["article_id"]), item)
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
