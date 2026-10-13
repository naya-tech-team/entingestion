"""Step 4: Normalize -> normalized/document.json (deterministic for input + pipeline_version).

With PII screening on (PII_SCREENING), the text is screened here, before anything is chunked or
embedded, and the source's policy applied (ingest.pii)."""

from __future__ import annotations

from typing import Any

from ingest import access, aws, pii, producer
from ingest.errors import ErrorCode, make_error
from ingest.ids import STAGING_FILES
from ingest.models.staging import Block, ExtractedContent, NormalizedDocument
from ingest.models.status import StepName
from ingest.normalize import clean_text, normalize_blocks
from ingest.steps.protocol import StepContext, StepOutput, step
from ingest.timeutil import iso_date

INPUT = STAGING_FILES["EXTRACT"]
OUTPUT = STAGING_FILES["NORMALIZE"]


def detector() -> pii.Detect:
    return pii.comprehend_detector(aws.comprehend())


def _screen(
    ctx: StepContext, source: str | None, title: str | None, blocks: list[Block]
) -> tuple[str | None, list[Block], dict[str, Any]]:
    """Apply the source's PII policy: (title, blocks, finding). Blocking raises E_PII_BLOCKED."""
    action = access.policy().pii_for(source)
    texts = [title or "", *(b.text for b in blocks)]
    found = pii.screen(texts, detector(), ctx.settings.pii.min_score)
    summary = found.summary(action)
    ctx.store.annotate(
        ctx.ref, {"pii_count": found.found, "pii_types": sorted(found.counts), "pii_action": action}
    )
    if found.found and action == "block":
        raise make_error(ErrorCode.PII_BLOCKED, f"PII found ({summary['types']}); source blocks it")
    if found.found and action == "redact":
        title = pii.redact(title, found.spans[0]) if title else title
        blocks = [
            b.model_copy(update={"text": pii.redact(b.text, spans)})
            for b, spans in zip(blocks, found.spans[1:], strict=True)
        ]
    return title, blocks, summary


@step(StepName.NORMALIZE, input_name=INPUT, requires=("producer_articles_table",))
def handler(ctx: StepContext) -> StepOutput:
    extracted = ExtractedContent.model_validate_json(ctx.staging.read(ctx.staging_prefix, INPUT))
    blocks = normalize_blocks(extracted.blocks)
    if not any(b.type != "heading" for b in blocks):
        raise make_error(ErrorCode.EMPTY_TEXT, "no text after extraction")

    row = producer.article(ctx.article_id) or {}
    metadata = dict(row.get("metadata") or {})
    raw_title = row.get("title") or extracted.title
    title = clean_text(raw_title) if raw_title else None
    finding = None
    if ctx.settings.pii.screening:
        title, blocks, finding = _screen(ctx, row.get("source"), title, blocks)
    doc = NormalizedDocument(
        article_id=ctx.article_id,
        doc_version=ctx.doc_version,
        pipeline_version=ctx.settings.pipeline_version,
        title=title,
        source=row.get("source"),
        process_id=ctx.message.process_id,
        # Set by the producer from the source's access policy; callers can't choose it.
        tenant_id=row.get("tenant_id"),
        metadata=metadata,
        published_at=iso_date(metadata.get("published_at")),
        pii=finding,
        blocks=blocks,
    )
    staged = ctx.staging.write(
        ctx.staging_prefix, OUTPUT, doc.model_dump_json().encode(), ctx.run_id, "application/json"
    )
    out: dict[str, Any] = {"blocks_in": len(extracted.blocks), "blocks_out": len(blocks)}
    if finding is not None:
        out |= {"pii_count": finding["count"], "pii_action": finding["action"]}
    return StepOutput(output_uri=staged.uri, output_etag=staged.etag, metrics=out)
