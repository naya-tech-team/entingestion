"""Detect the file type from its first bytes, with the file extension as a hint."""

from __future__ import annotations

import io
import zipfile
from enum import StrEnum

_HTML_MARKERS = (b"<!doctype html", b"<html", b"<head", b"<body", b"<p>", b"<div", b"<article")


class Kind(StrEnum):
    PDF = "application/pdf"
    DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    HTML = "text/html"
    MARKDOWN = "text/markdown"
    TEXT = "text/plain"
    UNKNOWN = "application/octet-stream"


def sniff(data: bytes, filename: str = "") -> Kind:  # noqa: PLR0911 - one return per type
    head = data[:4096]
    name = filename.lower()
    if head.startswith(b"%PDF-"):
        return Kind.PDF
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                if "word/document.xml" in zf.namelist():
                    return Kind.DOCX
        except zipfile.BadZipFile:
            return Kind.UNKNOWN
        return Kind.UNKNOWN
    if b"\x00" in head:
        return Kind.UNKNOWN
    lowered = head.lower().lstrip()
    if name.endswith((".html", ".htm")) or any(m in lowered for m in _HTML_MARKERS):
        return Kind.HTML
    if name.endswith((".md", ".markdown")):
        return Kind.MARKDOWN
    return Kind.TEXT
