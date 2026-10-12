"""Step 3: Extract -> extracted/content.json."""

from __future__ import annotations

from ingest.errors import ErrorCode, make_error
from ingest.extract.dispatch import extract
from ingest.extract.sniff import Kind, sniff
from ingest.ids import STAGING_FILES
from ingest.models.staging import ExtractedContent
from ingest.models.status import StepName
from ingest.steps.fetch import read_raw
from ingest.steps.protocol import StepContext, StepOutput, step

OUTPUT = STAGING_FILES["EXTRACT"]


@step(StepName.EXTRACT)
def handler(ctx: StepContext) -> StepOutput:
    data, key = read_raw(ctx)
    kind = sniff(data, key.rsplit("/", 1)[-1])
    if kind is Kind.UNKNOWN:
        raise make_error(ErrorCode.UNREADABLE, f"unrecognised file type for {key}")
    title, blocks, pages, extractor = extract(kind, data)
    chars = sum(len(b.text) for b in blocks)
    textless = chars < ctx.settings.ocr_min_chars_per_page * max(pages or 1, 1)
    if kind is Kind.PDF and textless and ctx.settings.ocr_enabled:
        # Scanned (no text layer): OCR finishes this step (brief §4.4).
        return StepOutput(
            metrics={"pages": pages or 0, "chars": chars, "route": "OCR"},
            result={"route": "OCR"},
            handoff=True,
        )
    content = ExtractedContent(
        article_id=ctx.article_id,
        doc_version=ctx.doc_version,
        content_type=kind.value,
        extractor=extractor,
        title=title,
        page_count=pages,
        blocks=blocks,
    )
    staged = ctx.staging.write(
        ctx.staging_prefix,
        OUTPUT,
        content.model_dump_json().encode(),
        ctx.run_id,
        "application/json",
    )
    return StepOutput(
        output_uri=staged.uri,
        output_etag=staged.etag,
        metrics={"blocks": len(blocks), "pages": pages or 0, "kind": kind.value},
        result={"route": "LAMBDA"},
    )
