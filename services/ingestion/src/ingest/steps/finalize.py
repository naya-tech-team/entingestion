"""Step 9: Finalize. INDEXED, lease released, process roll-up, article.indexed event. The older
versions' staged files are retired (ingest.lifecycle)."""

from __future__ import annotations

from aws_lambda_powertools.metrics import MetricUnit
from entcommon.timeutil import iso_now, parse_iso

from ingest import events, lifecycle, telemetry
from ingest.models.staging import Manifest
from ingest.models.status import ArticleStatus, RunOutcome, StepName
from ingest.status import keys
from ingest.steps.protocol import StepContext, StepOutput, logger, metrics, step
from ingest.steps.rollup import complete_process


def _announce(ctx: StepContext, manifest: Manifest, indexed_at: str) -> None:
    # run_id lets consumers drop the duplicate a retried Finalize sends.
    events.emit(
        "article.indexed",
        {
            "article_id": ctx.article_id,
            "doc_version": ctx.doc_version,
            "run_id": ctx.run_id,
            "process_id": ctx.message.process_id,
            "chunk_count": manifest.chunk_count,
            "pipeline_version": manifest.pipeline_version,
            "source_updated_at": ctx.message.source_updated_at,
            "indexed_at": indexed_at,
        },
    )


def _read_manifest(ctx: StepContext) -> Manifest:
    return Manifest.model_validate_json(
        ctx.staging.read(ctx.staging_prefix, ctx.target.manifest_file)
    )


def _already_finished(ctx: StepContext) -> StepOutput | None:
    """A retried Finalize whose first attempt ended the run but failed afterwards (the event or
    the process roll-up): redo only those."""
    run = ctx.store.get(keys.run_key(ctx.article_id, ctx.run_id)) or {}
    if run.get("outcome") != RunOutcome.INDEXED:
        return None
    latest = ctx.store.latest(ctx.article_id) or {}
    _announce(ctx, _read_manifest(ctx), str(latest.get("indexed_at") or iso_now()))
    completed = complete_process(ctx.store, ctx.message.process_id)
    return StepOutput(metrics={"reannounced": True, "process_completed": bool(completed)})


def _manifest(ctx: StepContext) -> str:
    return ctx.target.manifest_file


@step(
    StepName.FINALIZE,
    input_name=_manifest,
    reenter=_already_finished,
    requires=("producer_process_table",),
)
def handler(ctx: StepContext) -> StepOutput:
    manifest = _read_manifest(ctx)
    if ctx.lane is not None:
        # A shadow run: the table now holds this version; the article's status, events and the
        # process roll-up belong to its live run.
        ctx.store.finish(ctx.ref, ArticleStatus.INDEXED, RunOutcome.INDEXED)
        return StepOutput(metrics={"table": ctx.lane}, terminal=True)
    indexed_at = iso_now()
    ctx.store.finish(
        ctx.ref,
        ArticleStatus.INDEXED,
        RunOutcome.INDEXED,
        {
            "chunk_count": manifest.chunk_count,
            "indexed_at": indexed_at,
            "embedding_model": manifest.embedding_model,
        },
    )
    _announce(ctx, manifest, indexed_at)
    metrics.add_metric(name="Run_INDEXED", unit=MetricUnit.Count, value=1)  # failure ratio
    # Producer update -> searchable (the freshness SLO, docs/RUNBOOK.md "SLOs"), by trigger.
    # The SLO and its alarm use Trigger=STREAM: a replay or reconciler run of an old article
    # reports its age, which is worth seeing (a slow retry path) but isn't pipeline lag.
    lag = _freshness_lag(ctx.message.source_updated_at, indexed_at)
    if lag is not None:
        telemetry.emit("FreshnessLagSeconds", lag, MetricUnit.Seconds, Trigger=ctx.message.trigger)
    completed = complete_process(ctx.store, ctx.message.process_id)
    return StepOutput(
        metrics={
            "chunk_count": manifest.chunk_count,
            "process_completed": bool(completed),
            "old_staged_retired": _retire_old_versions(ctx),
        },
        terminal=True,
    )


def _retire_old_versions(ctx: StepContext) -> int:
    """The older versions' staged files turn noncurrent (lifecycle expires them); this version
    stays as the rebuild source. Best effort: the run is already finished."""
    if ctx.doc_version <= 1:
        return 0
    try:
        return lifecycle.retire_old_versions(
            ctx.settings.staging_bucket, ctx.article_id, ctx.staging_prefix
        )
    except Exception:
        logger.warning("old versions' staged files not retired", exc_info=True)
        return 0


def _freshness_lag(source_updated_at: str | None, indexed_at: str) -> float | None:
    if not source_updated_at:
        return None
    try:
        start = parse_iso(source_updated_at)
    except ValueError:
        return None
    return max(0.0, (parse_iso(indexed_at) - start).total_seconds())
