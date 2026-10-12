"""The step write protocol every step uses (brief §6.3).

    @step(StepName.CHUNK, input_name="normalized/document.json")
    def handler_impl(ctx: StepContext) -> StepOutput: ...

1. Start: one transaction moves LATEST to this step (only if this run still holds the lease and
   LATEST is at the previous step or this one) and marks the step item STARTED, attempt + 1.
   If the condition fails the run is stale: IngestStale, which the state machine ends quietly.
2. Work: the step reads the previous step's staged output and stages its own (Staging.write).
3. Success: step item SUCCEEDED with timings, URIs, ETag and metrics; the lease is extended.
4. Error: classified (errors.classify); step item FAILED; re-raised so Retry/Catch can match it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext

from ingest import aws
from ingest.config import Settings, get_settings
from ingest.errors import DeletePending, IngestStale, classify
from ingest.models.messages import IngestMessage
from ingest.models.status import StepName
from ingest.staging.io import Staging
from ingest.status.store import StatusStore, StepRef
from ingest.steps.handover import hand_over_delete
from ingest.vectorstore.targets import Target, target

logger = Logger()
tracer = Tracer()
metrics = Metrics()


@dataclass(frozen=True)
class StepContext:
    """What every step receives: the run identity plus clients."""

    message: IngestMessage
    article_id: str
    run_id: str
    doc_version: int
    staging_prefix: str
    mode: str  # "UPSERT", "DELETE", or "METADATA" (Index updates the rows' metadata only)
    settings: Settings
    store: StatusStore
    staging: Staging
    extra: dict[str, Any] = field(default_factory=dict)  # other event keys, e.g. job_id
    lane: str | None = None  # the one table a shadow run writes (reindex, rebuild, repair)
    slot: str | None = None  # the admission slot the dispatcher gave the run
    tables: tuple[str, ...] = ()  # every table this run writes, live first (from Claim)
    current_step: StepName | None = None
    _last_beat: list[float] = field(default_factory=lambda: [time.monotonic()])

    def heartbeat(self) -> None:
        """Extend the lease from inside a long step (Embed, page loops), at most every quarter
        lease. Raises IngestStale if another run has taken the article over."""
        now = time.monotonic()
        if self.current_step is None or now - self._last_beat[0] < self.settings.lease_seconds / 4:
            return
        self.store.touch_lease(self.ref, self.current_step)
        self._last_beat[0] = now

    @property
    def ref(self) -> StepRef:
        return StepRef(
            self.article_id,
            self.run_id,
            self.message.process_id,
            self.lane,
            self.doc_version,
            self.slot,
        )

    @property
    def targets(self) -> list[Target]:
        """Every vector table this run writes. The first is the one that must succeed (the live
        table, or a shadow run's table); the others are new tables still being filled, whose
        failures don't fail the run (the drift check repairs them)."""
        tables = self.tables or (self.message.target_table or self.settings.target_table,)
        return [target(t) for t in tables]

    @property
    def target(self) -> Target:
        """The table that decides the run: the live table, or a shadow run's table."""
        return self.targets[0]

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> StepContext:
        ctx = event["ctx"]
        settings = get_settings()
        return cls(
            message=IngestMessage.model_validate(ctx["message"]),
            article_id=ctx["article_id"],
            run_id=ctx["run_id"],
            doc_version=int(ctx["doc_version"]),
            staging_prefix=ctx["staging_prefix"],
            mode=ctx.get("mode", "UPSERT"),
            lane=ctx.get("lane"),
            slot=ctx.get("slot"),
            tables=tuple(ctx.get("targets") or ()),
            settings=settings,
            store=aws.status_store(),
            staging=Staging(aws.s3(), settings.staging_bucket),
            extra={k: v for k, v in event.items() if k != "ctx"},
        )


@dataclass
class StepOutput:
    output_uri: str | None = None
    output_etag: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)  # returned to the state machine
    terminal: bool = False  # the step already ended the run (Finalize, delete)
    # Another state finishes this step (Extract -> OCR): leave the step STARTED, don't succeed it.
    handoff: bool = False


StepFn = Callable[[StepContext], StepOutput]
Handler = Callable[[dict[str, Any], LambdaContext], dict[str, Any]]


ReentryFn = Callable[[StepContext], "StepOutput | None"]


def step(
    name: StepName,
    input_name: str | None = None,
    *,
    continues: bool = False,
    reenter: ReentryFn | None = None,
) -> Callable[[StepFn], Handler]:
    """continues: this handler finishes a step another state started (OcrCollect finishing
    Extract), so starting it again isn't a new attempt.

    reenter: called when start_step finds the run no longer holds the article. It may return
    an output (the step's work already happened, e.g. a Finalize retried after it finished the
    run); None means the run is stale."""

    def decorate(fn: StepFn) -> Handler:
        @logger.inject_lambda_context(clear_state=True)
        @tracer.capture_lambda_handler
        @metrics.log_metrics
        def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
            ctx = replace(StepContext.from_event(event), current_step=name)
            logger.append_keys(
                lane=ctx.lane,
                article_id=ctx.article_id,
                run_id=ctx.run_id,
                process_id=ctx.message.process_id,
                step=name.value,
                doc_version=ctx.doc_version,
            )
            input_uri = ctx.staging.uri(ctx.staging_prefix, input_name) if input_name else None
            # A stale run ends in the state machine's Stale state, which runs no code: give its
            # admission slot back here.
            try:
                ctx.store.start_step(ctx.ref, name, input_uri, count_attempt=not continues)
            except DeletePending as exc:
                hand_over_delete(ctx.store, ctx.ref)
                ctx.store.release_slot(ctx.slot, ctx.run_id)
                raise IngestStale(str(exc)) from None  # the state machine ends quietly
            except IngestStale:
                again = reenter(ctx) if reenter else None
                if again is None:
                    ctx.store.release_slot(ctx.slot, ctx.run_id)
                    raise  # ends quietly
                logger.info("step re-entered after the run ended", extra=again.metrics)
                return {"step": name.value, **again.result}
            started = time.monotonic()
            try:
                out = fn(ctx)
            except DeletePending as exc:
                hand_over_delete(ctx.store, ctx.ref)
                ctx.store.release_slot(ctx.slot, ctx.run_id)
                raise IngestStale(str(exc)) from None
            except IngestStale:
                ctx.store.release_slot(ctx.slot, ctx.run_id)
                raise
            except Exception as exc:
                err = classify(exc)
                elapsed = int((time.monotonic() - started) * 1000)
                ctx.store.fail_step(ctx.ref, name, elapsed, err)
                logger.warning(
                    "step failed",
                    extra={"error_code": err.code, "error_class": err.error_class.value},
                    exc_info=err is not exc,
                )
                metrics.add_metric(name=f"StepFailed_{name.value}", unit=MetricUnit.Count, value=1)
                if err is exc:
                    raise
                raise err from exc
            elapsed = int((time.monotonic() - started) * 1000)
            if out.handoff:
                logger.info("step handed off", extra={"duration_ms": elapsed, **out.result})
                return {"step": name.value, **out.result}
            ctx.store.succeed_step(
                ctx.ref,
                name,
                elapsed,
                output_uri=out.output_uri,
                output_etag=out.output_etag,
                metrics=out.metrics,
                terminal=out.terminal,
            )
            metrics.add_metric(
                name=f"StepDuration_{name.value}", unit=MetricUnit.Milliseconds, value=elapsed
            )
            logger.info("step succeeded", extra={"duration_ms": elapsed, **out.metrics})
            return {"step": name.value, **out.result}

        return handler

    return decorate
