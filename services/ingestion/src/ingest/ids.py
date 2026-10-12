"""Identifiers and keys (brief §4.3, §4.4, §5)."""

from __future__ import annotations

import hashlib
import json
import re

from ulid import ULID

EXECUTION_NAME_MAX = 80
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def new_run_id() -> str:
    return str(ULID())


def execution_name(
    article_id: str,
    *,
    content_sha256: str | None,
    process_id: str | None,
    source_updated_at: str | None,
    pipeline_version: str,
    replay_run_id: str | None = None,
) -> str:
    """Step Functions execution name: the dedupe key for one article version in one process.

    Step Functions names allow only [A-Za-z0-9_-] (no '#') and are unique per state machine for
    90 days, so StartExecution with the same name is the idempotency check: the same stream
    record delivered 3x starts one execution. process_id and source_updated_at are part of the
    key so an unchanged article joining another process, or a PATCH, still gets its own run
    (counted as SKIPPED_UNCHANGED, brief UC3/UC11). Replays append -r<run suffix>.
    """
    key = f"{content_sha256}|{process_id}|{source_updated_at}|{pipeline_version}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    name = f"{_NAME_UNSAFE.sub('_', article_id)}-{digest}"
    if replay_run_id:
        name += f"-r{replay_run_id[-8:]}"
    if len(name) > EXECUTION_NAME_MAX:
        raise ValueError(f"execution name too long ({len(name)} > {EXECUTION_NAME_MAX}): {name}")
    return name


def chunk_id(article_id: str, doc_version: int, ordinal: int) -> str:
    return hashlib.sha256(f"{article_id}:{doc_version}:{ordinal}".encode()).hexdigest()


def parent_chunk_id(article_id: str, doc_version: int, ordinal: int) -> str:
    """Id of a parent (section-level) chunk in parent-child chunking. Parents aren't embedded."""
    return hashlib.sha256(f"{article_id}:{doc_version}:p{ordinal}".encode()).hexdigest()


def embedding_cache_key(embed_text: str, model_id: str, dimension: int, normalize: bool) -> str:
    """sha256(embed_text)#model@dim@norm: the same text embeds differently per model, dimension
    and normalization, so every part of the request is in the key."""
    flag = "norm" if normalize else "raw"
    return f"{hashlib.sha256(embed_text.encode()).hexdigest()}#{model_id}@{dimension}@{flag}"


def meta_sha256(title: object, metadata: object, tenant_id: object) -> str:
    """Fingerprint of an article row's descriptive fields (title, metadata, tenant), so a change
    to them alone is noticed (Claim's METADATA action) without re-embedding the content."""
    canonical = json.dumps(
        {"title": title, "metadata": metadata or {}, "tenant_id": tenant_id},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def title_sha256(title: object) -> str:
    """The title is part of every chunk's embed_text: a new title is a content change."""
    return hashlib.sha256(json.dumps(title, default=str).encode()).hexdigest()


def staging_prefix(article_id: str, doc_version: int) -> str:
    return f"{article_id}/v{doc_version}/"


STAGING_FILES = {
    "EXTRACT": "extracted/content.json",
    "NORMALIZE": "normalized/document.json",
    "CHUNK": "chunks/chunks.jsonl",
    "EMBED": "embeddings/vectors.parquet",
    "VALIDATE": "_manifest.json",
}
