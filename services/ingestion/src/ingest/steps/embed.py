"""Step 6: Embed -> embeddings/<table>.parquet per vector table. Cache hits skip the model;
misses go to the table's embedding profile in batches of its batch size (Titan: one text per
call), with bounded concurrency. The function's reserved concurrency bounds the calls across
runs as well."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from aws_lambda_powertools.metrics import MetricUnit

from ingest import aws
from ingest.embedding.base import Embedder, Embedding
from ingest.embedding.cache import EmbeddingCache
from ingest.embedding.factory import make_embedder
from ingest.embedding.vectors import VectorRow, to_parquet
from ingest.errors import IngestStale
from ingest.ids import STAGING_FILES
from ingest.models.staging import Chunk
from ingest.models.status import StepName
from ingest.staging.io import StagedObject
from ingest.steps.protocol import StepContext, StepOutput, logger, metrics, step
from ingest.vectorstore.targets import Target

INPUT = STAGING_FILES["CHUNK"]
CACHE_FLUSH_EVERY = 25


def read_chunks(ctx: StepContext) -> list[Chunk]:
    body = ctx.staging.read(ctx.staging_prefix, INPUT).decode()
    return [Chunk.model_validate_json(line) for line in body.splitlines() if line.strip()]


def embedder(ctx: StepContext, t: Target) -> Embedder:
    return make_embedder(t.profile)


@step(StepName.EMBED, input_name=INPUT, requires=("cache_table",))
def handler(ctx: StepContext) -> StepOutput:
    chunks = read_chunks(ctx)
    primary, *others = ctx.targets
    staged, out = _embed_into(ctx, chunks, primary)
    for t in others:  # a table still being filled: its failure doesn't fail the run
        try:
            _, extra = _embed_into(ctx, chunks, t)
        except IngestStale:
            raise
        except Exception:
            logger.warning(
                "embedding for a new table failed", extra={"table": t.table}, exc_info=True
            )
            metrics.add_metric(name="DualWriteFailed", unit=MetricUnit.Count, value=1)
            continue
        out["bedrock_calls"] += extra["bedrock_calls"]
        out["bedrock_tokens"] += extra["bedrock_tokens"]
    return StepOutput(output_uri=staged.uri, output_etag=staged.etag, metrics=out)


def _embed_into(
    ctx: StepContext, chunks: list[Chunk], t: Target
) -> tuple[StagedObject, dict[str, Any]]:
    s = ctx.settings
    keys = {c.chunk_id: t.cache_key(c.embed_text) for c in chunks}
    cache = EmbeddingCache(aws.dynamodb(), aws.table(s.cache_table), s.retention.cache_ttl_days)
    cached = cache.get_many(keys.values())
    misses = [c for c in chunks if keys[c.chunk_id] not in cached]

    model = embedder(ctx, t)
    size = max(1, model.batch_size)
    batches = [misses[i : i + size] for i in range(0, len(misses), size)]
    fresh: dict[str, Embedding] = {}
    unsaved: dict[str, Embedding] = {}

    def save() -> None:
        cache.put_many({keys[cid]: emb for cid, emb in unsaved.items()})
        unsaved.clear()

    # Cache every result as it arrives, even after a call has failed, so a retry pays the model
    # only for what is left.
    failure: BaseException | None = None
    with ThreadPoolExecutor(max_workers=max(1, s.embedding.concurrency)) as pool:
        futures = {
            pool.submit(model.embed_documents, [c.embed_text for c in batch]): batch
            for batch in batches
        }
        for done in as_completed(futures):
            try:
                pairs = list(zip(futures[done], done.result(), strict=True))
            except Exception as exc:
                failure = failure or exc
                continue
            for c, emb in pairs:
                fresh[c.chunk_id] = unsaved[c.chunk_id] = emb
            if len(unsaved) >= CACHE_FLUSH_EVERY:
                save()
                ctx.heartbeat()
    save()
    if failure is not None:
        raise failure

    rows = []
    for c in chunks:
        hit = keys[c.chunk_id] in cached
        emb = cached[keys[c.chunk_id]] if hit else fresh[c.chunk_id]
        rows.append(VectorRow(c.chunk_id, emb.vector, emb.tokens, hit))
    staged = ctx.staging.write(
        ctx.staging_prefix,
        t.vectors_file,
        to_parquet(rows),
        ctx.run_id,
        "application/vnd.apache.parquet",
    )
    return staged, {
        "vectors": len(rows),
        "cache_hits": len(chunks) - len(misses),
        "bedrock_calls": len(batches),  # model calls (any provider)
        "bedrock_tokens": sum(e.tokens for e in fresh.values()),
    }
