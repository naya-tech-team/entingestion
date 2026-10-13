"""embedding_cache: sha256(embed_text)#model@dim@norm#<profile fingerprint> -> vector
(Target.cache_key), so unchanged chunks of a new version (and rebuilds) don't call the model
again (docs/USE_CASES.md UC2, UC10)."""

from __future__ import annotations

import array
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from ingest.embedding.base import Embedding
from ingest.timeutil import days, now_epoch

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import DynamoDBServiceResource, Table

_BATCH_GET = 100


def pack(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def unpack(blob: bytes) -> list[float]:
    values = array.array("f")
    values.frombytes(bytes(blob))
    return values.tolist()


class EmbeddingCache:
    def __init__(self, resource: DynamoDBServiceResource, table: Table, ttl_days: int) -> None:
        self.resource = resource
        self.table = table
        self.ttl_days = ttl_days

    def get_many(self, keys: Iterable[str]) -> dict[str, Embedding]:
        wanted = list(dict.fromkeys(keys))
        found: dict[str, Embedding] = {}
        for i in range(0, len(wanted), _BATCH_GET):
            request: dict[str, Any] = {
                self.table.name: {
                    "Keys": [{"cache_key": k} for k in wanted[i : i + _BATCH_GET]],
                    "ProjectionExpression": "cache_key, vec, tokens",
                }
            }
            while request:
                resp = self.resource.batch_get_item(RequestItems=request)
                for item in resp.get("Responses", {}).get(self.table.name, []):
                    blob: Any = item["vec"]
                    raw = blob.value if hasattr(blob, "value") else blob  # boto3 Binary wrapper
                    tokens: Any = item.get("tokens", 0)
                    found[str(item["cache_key"])] = Embedding(unpack(bytes(raw)), int(tokens))
                request = dict(resp.get("UnprocessedKeys") or {})
        return found

    def put_many(self, entries: Mapping[str, Embedding]) -> None:
        expires = now_epoch() + days(self.ttl_days)
        with self.table.batch_writer(overwrite_by_pkeys=["cache_key"]) as batch:
            for key, emb in entries.items():
                batch.put_item(
                    Item={
                        "cache_key": key,
                        "vec": pack(emb.vector),
                        "tokens": emb.tokens,
                        "expires_at_epoch": expires,
                    }
                )

    def delete_many(self, keys: Iterable[str]) -> None:
        """Remove entries (a deleted article's vectors); missing keys are fine."""
        with self.table.batch_writer(overwrite_by_pkeys=["cache_key"]) as batch:
            for key in dict.fromkeys(keys):
                batch.delete_item(Key={"cache_key": key})
