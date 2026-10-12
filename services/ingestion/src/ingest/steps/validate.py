"""Step 7: Validate -> _manifest.json. The gate before pgvector (requirement 4): Index refuses
anything without a passing manifest."""

from __future__ import annotations

import math

from aws_lambda_powertools.metrics import MetricUnit

from ingest.embedding.vectors import VectorRow, from_parquet
from ingest.errors import IngestError, make_error, validation_code
from ingest.ids import STAGING_FILES
from ingest.models.staging import CheckResult, Chunk, Manifest
from ingest.models.status import StepName
from ingest.staging.io import StagedObject
from ingest.steps.embed import read_chunks
from ingest.steps.protocol import StepContext, StepOutput, logger, metrics, step
from ingest.timeutil import iso_now
from ingest.vectorstore.targets import Target

OUTPUT = STAGING_FILES["VALIDATE"]
_NORM_TOLERANCE = 0.02


def run_checks(
    *,
    chunks: list[Chunk],
    vectors: list[VectorRow],
    article_id: str,
    doc_version: int,
    dimension: int,
    max_chunk_tokens: int,
    max_input_tokens: int = 8192,
    normalized: bool = True,
    model_tokens_exact: bool = True,
) -> dict[str, CheckResult]:
    """max_input_tokens: the embedding model's input limit (the profile's, capped by the model
    family's). A chunk over it would be truncated by the model (Cohere truncates at the end), so
    it fails "oversized" instead of being embedded partly. Our token counts are the chunker's
    (cl100k), an approximation of the model's own. model_tokens_exact: the model reported each
    input's tokens (Titan), so those are checked too; batch APIs only report a total.
    normalized: the profile asks for unit-length vectors (otherwise their length isn't checked)."""
    ids = [c.chunk_id for c in chunks]
    vector_ids = [v.chunk_id for v in vectors]

    def check(ok: bool, detail: str) -> CheckResult:
        return CheckResult(passed=ok, detail=None if ok else detail)

    bad_dims = [v.chunk_id for v in vectors if len(v.embedding) != dimension]
    bad_values = [v.chunk_id for v in vectors if not all(math.isfinite(x) for x in v.embedding)]
    bad_norms = [
        v.chunk_id
        for v in vectors
        if normalized and abs(math.sqrt(sum(x * x for x in v.embedding)) - 1.0) > _NORM_TOLERANCE
    ]
    empty = [c.chunk_id for c in chunks if not c.text.strip() or not c.embed_text.strip()]
    limit = min(max_chunk_tokens, max_input_tokens)
    oversized = [c.chunk_id for c in chunks if c.token_count > limit] + [
        v.chunk_id for v in vectors if model_tokens_exact and v.model_tokens > max_input_tokens
    ]
    wrong_owner = [
        c.chunk_id for c in chunks if c.article_id != article_id or c.doc_version != doc_version
    ]
    return {
        "nonempty": check(len(chunks) > 0, "no chunks"),
        "count": check(
            len(vectors) == len(chunks), f"{len(vectors)} vectors for {len(chunks)} chunks"
        ),
        "unique": check(len(set(ids)) == len(ids), "duplicate chunk_id"),
        "ids": check(set(vector_ids) == set(ids), "vectors and chunks have different ids"),
        "dimension": check(not bad_dims, f"{len(bad_dims)} vectors not {dimension}-d"),
        "finite": check(not bad_values, f"{len(bad_values)} vectors with NaN/inf"),
        "normalized": check(not bad_norms, f"{len(bad_norms)} vectors not unit length"),
        "empty": check(not empty, f"{len(empty)} empty chunks"),
        "oversized": check(not oversized, f"{len(oversized)} chunks over {limit} tokens"),
        "metadata": check(not wrong_owner, f"{len(wrong_owner)} chunks with wrong article/version"),
    }


@step(StepName.VALIDATE, input_name=STAGING_FILES["EMBED"])
def handler(ctx: StepContext) -> StepOutput:
    chunks = read_chunks(ctx)
    primary, *others = ctx.targets
    staged = _validate(ctx, chunks, primary)
    for t in others:  # a table still being filled: skip it if Embed couldn't stage it
        if ctx.staging.etag(ctx.staging_prefix, t.vectors_file) is None:
            continue
        try:
            _validate(ctx, chunks, t)
        except IngestError:
            logger.warning("vectors for a new table failed validation", extra={"table": t.table})
            metrics.add_metric(name="DualWriteFailed", unit=MetricUnit.Count, value=1)
    return StepOutput(
        output_uri=staged.uri,
        output_etag=staged.etag,
        metrics={"chunks": len(chunks), "tables": len(ctx.targets)},
        result={"chunk_count": len(chunks)},
    )


def _validate(ctx: StepContext, chunks: list[Chunk], t: Target) -> StagedObject:
    s = ctx.settings
    vectors = from_parquet(ctx.staging.read(ctx.staging_prefix, t.vectors_file))
    checks = run_checks(
        chunks=chunks,
        vectors=vectors,
        article_id=ctx.article_id,
        doc_version=ctx.doc_version,
        dimension=t.dimension,
        max_chunk_tokens=s.max_chunk_tokens,
        max_input_tokens=t.profile.input_token_limit,
        normalized=t.normalize,
        model_tokens_exact=t.profile.per_input_tokens,
    )
    passed = all(c.passed for c in checks.values())
    manifest = Manifest(
        article_id=ctx.article_id,
        doc_version=ctx.doc_version,
        pipeline_version=s.pipeline_version,
        embedding_model=t.model_id,
        dimension=t.dimension,
        chunk_count=len(chunks),
        vector_count=len(vectors),
        checks=checks,
        passed=passed,
        created_at=iso_now(),
    )
    staged = ctx.staging.write(
        ctx.staging_prefix,
        t.manifest_file,
        manifest.model_dump_json().encode(),
        ctx.run_id,
        "application/json",
    )
    if not passed:
        failed = next(name for name, c in checks.items() if not c.passed)
        raise make_error(validation_code(failed), checks[failed].detail or failed)
    return staged
