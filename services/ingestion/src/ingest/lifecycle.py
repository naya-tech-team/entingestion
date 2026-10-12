"""What a deleted article leaves behind, and old versions' staged files (review M3.5).

Delete (the Index step of a delete run): after the rows are gone, the article's embedding-cache
entries (keys from its last staged chunks, for every vector table) and every staged object
version under <article_id>/ are removed for good, so a deleted article's text doesn't outlive
it in staging or the cache. A cache entry shared with another article's identical text only
costs that article one Bedrock call later.

New version (Finalize): the previous versions' staged files are deleted. In the versioned bucket
they become noncurrent and the staging lifecycle expires them after
staging_noncurrent_expiration_days; the current version stays, as the source of rebuilds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ingest import aws
from ingest.config import get_settings
from ingest.embedding.cache import EmbeddingCache
from ingest.ids import STAGING_FILES
from ingest.models.staging import Chunk
from ingest.staging.io import Staging
from ingest.vectorstore.targets import TARGETS

if TYPE_CHECKING:
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef

_DELETE_BATCH = 1000  # S3 DeleteObjects limit


def article_prefix(article_id: str) -> str:
    return f"{article_id}/"  # the slash: art_1 must not match art_10


def cache_keys(staging: Staging, version_prefix: str | None) -> list[str]:
    """Embedding-cache keys of a version's chunks, for every vector table (none if its chunks
    aren't staged)."""
    if not version_prefix or staging.etag(version_prefix, STAGING_FILES["CHUNK"]) is None:
        return []
    body = staging.read(version_prefix, STAGING_FILES["CHUNK"]).decode()
    chunks = [Chunk.model_validate_json(line) for line in body.splitlines() if line.strip()]
    return sorted({t.cache_key(c.embed_text) for t in TARGETS.values() for c in chunks})


def purge_cache(keys: list[str]) -> int:
    if not keys:
        return 0
    settings = get_settings()
    cache = EmbeddingCache(aws.dynamodb(), aws.table(settings.cache_table), settings.cache_ttl_days)
    cache.delete_many(keys)
    return len(keys)


def purge_staging(bucket: str, article_id: str) -> int:
    """Delete every version (and delete marker) of every object under the article's prefix."""
    s3 = aws.s3()
    doomed: list[ObjectIdentifierTypeDef] = []
    for page in s3.get_paginator("list_object_versions").paginate(
        Bucket=bucket, Prefix=article_prefix(article_id)
    ):
        for v in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]:
            doomed.append({"Key": str(v["Key"]), "VersionId": str(v["VersionId"])})
    _delete(bucket, doomed)
    return len(doomed)


def retire_old_versions(bucket: str, article_id: str, current_prefix: str) -> int:
    """Delete the current objects of the article's other versions (they turn noncurrent and
    expire by lifecycle). Returns how many."""
    s3 = aws.s3()
    old: list[ObjectIdentifierTypeDef] = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=article_prefix(article_id)
    ):
        old += [
            {"Key": str(o["Key"])}
            for o in page.get("Contents", [])
            if not str(o["Key"]).startswith(current_prefix)
        ]
    _delete(bucket, old)
    return len(old)


def _delete(bucket: str, objects: list[ObjectIdentifierTypeDef]) -> None:
    for i in range(0, len(objects), _DELETE_BATCH):
        resp = aws.s3().delete_objects(
            Bucket=bucket, Delete={"Objects": objects[i : i + _DELETE_BATCH], "Quiet": True}
        )
        errors = resp.get("Errors") or []
        if errors:
            raise RuntimeError(f"{len(errors)} staged object(s) not deleted: {errors[0]}")
