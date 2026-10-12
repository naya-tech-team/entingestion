"""DOCX via mammoth (semantic HTML: headings, lists, tables), then the HTML block walker."""

from __future__ import annotations

import io

import mammoth

from ingest.errors import ErrorCode, make_error
from ingest.extract.htmlblocks import html_to_blocks
from ingest.models.staging import Block


def extract(data: bytes) -> tuple[str | None, list[Block]]:
    try:
        result = mammoth.convert_to_html(io.BytesIO(data))
    except Exception as exc:  # mammoth raises plain exceptions for corrupt files
        raise make_error(ErrorCode.UNREADABLE, f"DOCX: {exc}") from exc
    _, blocks = html_to_blocks(f"<html><body>{result.value}</body></html>")
    title = next((b.text for b in blocks if b.type == "heading" and b.level == 1), None)
    return title, blocks
