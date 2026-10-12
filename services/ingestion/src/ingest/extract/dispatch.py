"""Extract canonical blocks from raw files (brief §4.4 step 3): picks the extractor by file type."""

from __future__ import annotations

from ingest.errors import ErrorCode, make_error
from ingest.extract import docx, html, pdf, text
from ingest.extract.sniff import Kind
from ingest.models.staging import Block


def extract(kind: Kind, data: bytes) -> tuple[str | None, list[Block], int | None, str]:
    """Returns (title, blocks, page_count, extractor name)."""
    if kind is Kind.HTML:
        title, blocks = html.extract(data)
        return title, blocks, None, "trafilatura"
    if kind is Kind.DOCX:
        title, blocks = docx.extract(data)
        return title, blocks, None, "mammoth"
    if kind is Kind.PDF:
        title, blocks, pages = pdf.extract(data)
        return title, blocks, pages, "pdfplumber"
    if kind is Kind.MARKDOWN:
        return None, text.extract_markdown(text.decode(data)), None, "markdown"
    if kind is Kind.TEXT:
        return None, text.extract_plain(text.decode(data)), None, "text"
    raise make_error(ErrorCode.UNREADABLE, f"unsupported file type {kind}")
