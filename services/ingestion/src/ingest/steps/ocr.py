"""OCR for scanned or very large PDFs (brief §4.4, UC12): Amazon Textract async, orchestrated by
the state machine instead of a long-running worker.

    OcrStart   -> StartDocumentAnalysis (LAYOUT + TABLES) on the producer's object version;
                  Textract writes its result to staging (<prefix>ocr/<JobId>/1..n), which is the
                  page checkpoint. LAYOUT gives reading order (columns), headings and lists.
    OcrWait    -> (Wait state)
    OcrPoll    -> GetDocumentAnalysis status; extends the lease while Textract works
    OcrCollect -> the Extract step's output (extracted/content.json) from the staged result

All of it belongs to the Extract step: OcrStart (re)opens it and OcrCollect succeeds it, so the
step item, resume and record counts are the same as for a text PDF. A retried OcrStart reuses the
job (ClientRequestToken = run id), and OcrCollect only reads staged files, so it can rerun.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext

from ingest import aws
from ingest.config import get_settings
from ingest.errors import ErrorCode, IngestStale, classify, make_error
from ingest.extract.ocr import blocks_from_textract, title_of
from ingest.ids import STAGING_FILES
from ingest.models.staging import ExtractedContent
from ingest.models.status import StepName
from ingest.staging.io import parse_s3_uri
from ingest.steps.protocol import StepContext, StepOutput, step

logger = Logger()
tracer = Tracer()

OUTPUT = STAGING_FILES["EXTRACT"]
OCR_DIR = "ocr"
# Reading order, headings and lists (LAYOUT) and table cells (TABLES), extract/ocr.py.
FEATURES: list[Literal["LAYOUT", "TABLES"]] = ["LAYOUT", "TABLES"]


def _context(event: dict[str, Any]) -> StepContext:
    ctx = StepContext.from_event(event)
    logger.append_keys(
        article_id=ctx.article_id, run_id=ctx.run_id, step="EXTRACT", doc_version=ctx.doc_version
    )
    return ctx


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
def start(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    ctx = _context(event)
    msg = ctx.message
    if not msg.s3_uri:
        raise make_error(ErrorCode.BUG, "UPSERT message without s3_uri")
    # Large PDFs come straight from Fetch; scanned ones after Extract handed off (not a retry).
    handed_off = (ctx.store.latest(ctx.article_id) or {}).get("stage") == StepName.EXTRACT
    with _stale_ends_the_run(ctx):  # IngestStale -> ends quietly
        ctx.store.start_step(ctx.ref, StepName.EXTRACT, msg.s3_uri, count_attempt=not handed_off)
    bucket, key = parse_s3_uri(msg.s3_uri)
    s3_object: dict[str, str] = {"Bucket": bucket, "Name": key}
    if msg.s3_version_id:
        s3_object["Version"] = msg.s3_version_id
    try:
        resp = aws.textract().start_document_analysis(
            DocumentLocation={"S3Object": s3_object},  # type: ignore[typeddict-item]
            FeatureTypes=FEATURES,
            ClientRequestToken=ctx.run_id,  # a retry gets the same job back
            JobTag=ctx.article_id[:64],
            OutputConfig={
                "S3Bucket": ctx.settings.staging_bucket,
                "S3Prefix": f"{ctx.staging_prefix}{OCR_DIR}",
            },
        )
    except Exception as exc:
        raise classify(exc) from exc
    logger.info("OCR started", extra={"job_id": resp["JobId"]})
    return {"job_id": resp["JobId"]}


@contextmanager
def _stale_ends_the_run(ctx: StepContext) -> Iterator[None]:
    """The state machine ends a stale run in a state that runs no code: free its slot here."""
    try:
        yield
    except IngestStale:
        ctx.store.release_slot(ctx.slot, ctx.run_id)
        raise


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
def poll(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """One status check. `polls` counts them (the state machine carries it): a job that is
    still running after OCR_MAX_POLLS checks fails the run as E_TIMEOUT (retryable later)
    instead of looping until the execution times out."""
    ctx = _context(event)
    job_id = str(event["job_id"])
    polls = int(event.get("polls") or 0) + 1
    with _stale_ends_the_run(ctx):
        ctx.store.touch_lease(ctx.ref, StepName.EXTRACT)  # still ours; keep it while it works
    try:
        resp = aws.textract().get_document_analysis(JobId=job_id, MaxResults=1)
    except Exception as exc:
        raise classify(exc) from exc
    status = resp["JobStatus"]
    if status == "FAILED":
        raise make_error(
            ErrorCode.UNREADABLE, f"Textract job {job_id} failed: {resp.get('StatusMessage', '')}"
        )
    limit = ctx.settings.ocr_max_polls
    if status == "IN_PROGRESS" and polls >= limit:
        raise make_error(
            ErrorCode.TIMEOUT,
            f"Textract job {job_id} still running after {polls} status checks"
            f" (OCR_MAX_POLLS={limit}); the reconciler retries the article later",
        )
    return {"job_id": job_id, "status": status, "polls": polls}


@step(StepName.EXTRACT, continues=True)
def collect(ctx: StepContext) -> StepOutput:
    job_id = str(ctx.extra["job_id"])
    s3 = aws.s3()
    bucket = get_settings().staging_bucket
    prefix = f"{ctx.staging_prefix}{OCR_DIR}/{job_id}/"
    keys: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys += [str(o["Key"]) for o in page.get("Contents", [])]
    parts = sorted((k for k in keys if k.rsplit("/", 1)[-1].isdigit()), key=_part_number)
    if not parts:
        raise make_error(ErrorCode.UNREADABLE, f"no Textract output under s3://{bucket}/{prefix}")
    blocks, pages = blocks_from_textract(
        json.loads(s3.get_object(Bucket=bucket, Key=k)["Body"].read()) for k in parts
    )
    content = ExtractedContent(
        article_id=ctx.article_id,
        doc_version=ctx.doc_version,
        content_type="pdf",
        extractor="textract",
        title=title_of(blocks),
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
        metrics={"blocks": len(blocks), "pages": pages, "kind": "pdf", "extractor": "textract"},
    )


def _part_number(key: str) -> int:
    return int(key.rsplit("/", 1)[-1])
