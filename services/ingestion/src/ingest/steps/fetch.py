"""Step 2: Fetch. Read the exact raw version and verify its SHA-256 against the producer row.

The producer verifies files before writing rows, so a missing object or a hash mismatch means its
contract broke: permanent (E_SOURCE_MISSING / E_SOURCE_CHANGED), no waiting retry.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ingest import aws
from ingest.errors import ErrorCode, make_error
from ingest.extract.sniff import sniff
from ingest.models.status import StepName
from ingest.staging.io import parse_s3_uri
from ingest.steps.protocol import StepContext, StepOutput, step

_CHUNK = 8 * 1024 * 1024


def read_raw(ctx: StepContext) -> tuple[bytes, str]:
    """The raw object's bytes (verified) and its key."""
    msg = ctx.message
    args = _raw_args(ctx)
    obj = aws.s3().get_object(**args)
    size = int(obj["ContentLength"])
    if size > ctx.settings.lambda_max_source_bytes:
        raise make_error(ErrorCode.TOO_LARGE, f"{size} bytes exceeds the Lambda limit")
    digest = hashlib.sha256()
    parts = []
    for chunk in obj["Body"].iter_chunks(_CHUNK):
        digest.update(chunk)
        parts.append(chunk)
    if digest.hexdigest() != msg.content_sha256:
        raise make_error(
            ErrorCode.SOURCE_CHANGED, f"sha256 {digest.hexdigest()} != {msg.content_sha256}"
        )
    return b"".join(parts), str(args["Key"])


def _raw_args(ctx: StepContext) -> dict[str, Any]:
    msg = ctx.message
    if not msg.s3_uri or not msg.content_sha256:
        raise make_error(ErrorCode.BUG, "UPSERT message without s3_uri/content_sha256")
    bucket, key = parse_s3_uri(msg.s3_uri)
    args: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if msg.s3_version_id:
        args["VersionId"] = msg.s3_version_id
    return args


def _route_large(ctx: StepContext) -> int | None:
    """For a source over the Lambda limit: its size if it's a PDF OCR can take (verified by
    streaming, never held in memory), else a permanent E_TOO_LARGE. None if it fits Lambda."""
    args = _raw_args(ctx)
    size = int(aws.s3().head_object(**args)["ContentLength"])
    if size <= ctx.settings.lambda_max_source_bytes:
        return None
    if not ctx.settings.ocr.enabled:
        raise make_error(ErrorCode.TOO_LARGE, f"{size} bytes exceeds the Lambda limit (OCR off)")
    if size > ctx.settings.ocr.max_bytes:
        raise make_error(ErrorCode.TOO_LARGE, f"{size} bytes exceeds the OCR limit")
    body = aws.s3().get_object(**args)["Body"]
    digest = hashlib.sha256()
    head = b""
    for chunk in body.iter_chunks(_CHUNK):
        head = head or chunk[:5]
        digest.update(chunk)
    if head != b"%PDF-":
        raise make_error(ErrorCode.TOO_LARGE, f"{size} bytes exceeds the Lambda limit")
    if digest.hexdigest() != ctx.message.content_sha256:
        raise make_error(
            ErrorCode.SOURCE_CHANGED, f"sha256 {digest.hexdigest()} != {ctx.message.content_sha256}"
        )
    return size


@step(StepName.FETCH)
def handler(ctx: StepContext) -> StepOutput:
    large = _route_large(ctx)
    if large is not None:  # a big PDF: Textract reads it from S3 (the OCR route)
        return StepOutput(
            metrics={"bytes": large, "kind": "pdf", "route": "OCR"},
            result={"kind": "pdf", "route": "OCR"},
        )
    data, key = read_raw(ctx)
    kind = sniff(data, key.rsplit("/", 1)[-1])
    return StepOutput(
        metrics={"bytes": len(data), "kind": kind.value},
        result={"kind": kind.value, "route": "LAMBDA"},
    )
