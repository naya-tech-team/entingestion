"""Step 8: Index. Only a passing manifest for this exact version reaches pgvector.

Three modes:
    UPSERT    write this version into every write table and swap it live
    METADATA  same content, other metadata or tenant: rewrite those on the article's active rows
              in every write table and end the run (Claim's METADATA action)
    DELETE    remove the article's rows everywhere, its embedding-cache entries and staged files
              (ingest.lifecycle), and end the run

The article's descriptive fields (tenant, source, published_at, producer metadata) come from
the producer's row at index time, so a replay or rebuild from older staged chunks still writes
the current ones. Articles whose row has no tenant are indexed under 'default'.
"""

from __future__ import annotations

from typing import Any, Literal, overload

from aws_lambda_powertools.metrics import MetricUnit
from entcommon.timeutil import iso_now

from ingest import lifecycle, producer
from ingest.embedding.vectors import from_parquet
from ingest.errors import IngestStale, make_error, validation_code
from ingest.models.staging import Chunk, Manifest
from ingest.models.status import ArticleStatus, RunOutcome, StepName
from ingest.steps.embed import read_chunks
from ingest.steps.protocol import StepContext, StepOutput, logger, metrics, step
from ingest.steps.rollup import complete_process
from ingest.timeutil import iso_date
from ingest.vectorstore import index as vector_index
from ingest.vectorstore.index import DEFAULT_TENANT, ArticleAttributes
from ingest.vectorstore.targets import TARGETS, Target


def article_attributes(article_id: str, chunks: list[Chunk] | None = None) -> ArticleAttributes:
    """Tenant, filters and metadata from the producer's row; without a row (deleted meanwhile),
    what the staged chunks recorded."""
    row = producer.article(article_id)
    if row is not None:
        metadata = dict(row.get("metadata") or {})
        return ArticleAttributes(
            tenant_id=str(row.get("tenant_id") or DEFAULT_TENANT),
            source=row.get("source"),
            published_at=iso_date(metadata.get("published_at")),
            metadata=metadata,
        )
    staged = chunks[0].metadata if chunks else {}
    return ArticleAttributes(
        tenant_id=str(staged.get("tenant_id") or DEFAULT_TENANT),
        source=staged.get("source"),
        published_at=staged.get("published_at"),
        metadata=dict(staged.get("article") or {}),
    )


def _delete(ctx: StepContext) -> StepOutput:
    """Rows from every vector table, then the article's cache entries and staged files (all
    versions, for good): nothing of a deleted article stays behind. Each part is idempotent, so a
    retry after a partial failure finishes the job."""
    removed = vector_index.delete_everywhere(ctx.article_id, ctx.doc_version)
    keys = lifecycle.cache_keys(ctx.staging, ctx.staging_prefix)  # before the files go
    cached = lifecycle.purge_cache(keys)
    staged = lifecycle.purge_staging(ctx.settings.staging_bucket, ctx.article_id)
    ctx.store.finish(ctx.ref, ArticleStatus.DELETED, RunOutcome.DELETED, {"chunk_count": 0})
    return StepOutput(
        metrics={"rows_deleted": removed, "cache_deleted": cached, "staged_deleted": staged},
        terminal=True,
    )


def _metadata(ctx: StepContext) -> StepOutput:
    """Rewrite the article's descriptive columns in place; the content is unchanged."""
    attributes = article_attributes(ctx.article_id)
    primary, *others = ctx.targets
    updated = {primary.table: _update(primary, ctx.article_id, attributes)}
    for t in others:  # a table still being filled: the drift check repairs a failure
        try:
            updated[t.table] = _update(t, ctx.article_id, attributes)
        except Exception:
            logger.warning("metadata update in a new table failed", extra={"table": t.table})
            metrics.add_metric(name="DualWriteFailed", unit=MetricUnit.Count, value=1)
    _retag_others(ctx, attributes)
    ctx.store.finish(
        ctx.ref,
        ArticleStatus.INDEXED,
        RunOutcome.METADATA_UPDATED,
        {"metadata_updated_at": iso_now()},
        counter="skipped",
    )
    completed = complete_process(ctx.store, ctx.message.process_id)
    return StepOutput(
        metrics={"rows_updated": updated, "process_completed": bool(completed)}, terminal=True
    )


def _update(t: Target, article_id: str, attributes: ArticleAttributes) -> int:
    return vector_index.get_vector_store(t.table).update_metadata(article_id, attributes)


def _retag_others(ctx: StepContext, attributes: ArticleAttributes) -> None:
    """Tables this run doesn't write may still hold the article's rows from before it moved to
    another tenant (whose registry writes other tables): they get the new tenant, so the old
    tenant's searches no longer see them. Best effort (a table not created yet is skipped);
    usually nothing matches."""
    written = {t.table for t in ctx.targets}
    for table in TARGETS:
        if table in written:
            continue
        try:
            changed = vector_index.get_vector_store(table).retag_tenant(
                ctx.article_id, attributes.tenant_id
            )
        except Exception:
            logger.warning("retagging rows in another table failed", extra={"table": table})
            continue
        if changed:
            logger.warning(
                "rows of a former tenant retagged", extra={"table": table, "rows": changed}
            )


def _manifest(ctx: StepContext) -> str:
    return ctx.target.manifest_file


@step(
    StepName.INDEX,
    input_name=_manifest,
    requires=("db.host", "cache_table", "producer_articles_table", "producer_process_table"),
)
def handler(ctx: StepContext) -> StepOutput:
    if ctx.mode == "DELETE":
        return _delete(ctx)
    if ctx.mode == "METADATA":
        return _metadata(ctx)
    chunks = read_chunks(ctx)
    attributes = article_attributes(ctx.article_id, chunks)
    primary, *others = ctx.targets
    written = _index_into(ctx, chunks, attributes, primary, required=True)
    out: dict[str, Any] = {"rows_written": written.written, "rows_deleted_old": written.deleted_old}
    out["tables"] = [primary.table]
    for t in others:  # a table still being filled
        try:
            if _index_into(ctx, chunks, attributes, t, required=False) is not None:
                out["tables"].append(t.table)
        except IngestStale:
            raise
        except Exception:
            logger.warning(
                "indexing into a new table failed", extra={"table": t.table}, exc_info=True
            )
            metrics.add_metric(name="DualWriteFailed", unit=MetricUnit.Count, value=1)
    if ctx.lane is None:  # a live run: its tenant's tables are the article's tables now
        _retag_others(ctx, attributes)
    return StepOutput(metrics=out, result={"chunk_count": written.written})


@overload
def _index_into(
    ctx: StepContext,
    chunks: list[Chunk],
    attributes: ArticleAttributes,
    t: Target,
    *,
    required: Literal[True],
) -> vector_index.IndexResult: ...
@overload
def _index_into(
    ctx: StepContext,
    chunks: list[Chunk],
    attributes: ArticleAttributes,
    t: Target,
    *,
    required: bool,
) -> vector_index.IndexResult | None: ...
def _index_into(
    ctx: StepContext,
    chunks: list[Chunk],
    attributes: ArticleAttributes,
    t: Target,
    *,
    required: bool,
) -> vector_index.IndexResult | None:
    """Write this version into one table. A table whose manifest is missing or failed is an
    error for the table that must succeed, and skipped otherwise."""
    if not required and ctx.staging.etag(ctx.staging_prefix, t.manifest_file) is None:
        return None
    manifest = Manifest.model_validate_json(ctx.staging.read(ctx.staging_prefix, t.manifest_file))
    if not manifest.passed or manifest.doc_version != ctx.doc_version:
        if not required:
            return None
        raise make_error(
            validation_code("manifest"), "manifest missing, failed, or for another version"
        )
    vectors = {
        v.chunk_id: v for v in from_parquet(ctx.staging.read(ctx.staging_prefix, t.vectors_file))
    }
    rows = [
        vector_index.IndexRow(
            chunk=c,
            embedding=vectors[c.chunk_id].embedding,
            process_id=ctx.message.process_id,
            attributes=attributes,
            embedding_model=t.model_id,
            pipeline_version=ctx.settings.pipeline_version,
        )
        for c in chunks
    ]
    result = vector_index.get_vector_store(t.table).write_and_swap(
        ctx.article_id, ctx.doc_version, rows
    )
    if ctx.lane is None:  # a shadow run records its table when it finishes
        ctx.store.record_table_version(ctx.article_id, t.table, ctx.doc_version)
    return result
