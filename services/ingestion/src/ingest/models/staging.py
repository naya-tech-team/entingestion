"""Documents written to the staging bucket between steps."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Staged(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BlockType(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    CODE = "code"


class Block(_Staged):
    type: BlockType
    text: str
    heading_path: list[str] = Field(default_factory=list)
    page: int | None = None
    level: int | None = None  # heading level, 1-6


class ExtractedContent(_Staged):
    """extracted/content.json: the extractor's raw output."""

    article_id: str
    doc_version: int
    content_type: str
    extractor: str
    title: str | None = None
    page_count: int | None = None
    blocks: list[Block]


class NormalizedDocument(_Staged):
    """normalized/document.json: deterministic for (input, pipeline_version)."""

    article_id: str
    doc_version: int
    pipeline_version: str
    title: str | None = None
    source: str | None = None
    process_id: str | None = None
    tenant_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    published_at: str | None = None  # ISO-8601 UTC when known
    # PII screening result (Normalize, when pii_screening is on): types found, count, action.
    pii: dict[str, Any] | None = None
    blocks: list[Block]

    def block_offsets(self) -> list[int]:
        """Where each block starts in text() (chunk citations are offsets into it)."""
        offsets, at = [], 0
        for block in self.blocks:
            offsets.append(at)
            at += len(block.text) + len(BLOCK_SEPARATOR)
        return offsets

    def text(self) -> str:
        """The normalized text: every block's text, blank-line separated."""
        return BLOCK_SEPARATOR.join(b.text for b in self.blocks)


BLOCK_SEPARATOR = "\n\n"


class Chunk(_Staged):
    """One line of chunks/chunks.jsonl."""

    chunk_id: str
    article_id: str
    doc_version: int
    ordinal: int
    # Parent-child chunking (chunk_parent_child): the section-level parent this child belongs
    # to, and the parent's text, which search returns as the child's context.
    parent_id: str | None = None
    parent_text: str | None = None
    text: str
    embed_text: str
    heading_path: list[str] = Field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    # Citation: [char_start, char_end) of the normalized text (NormalizedDocument.text()).
    char_start: int | None = None
    char_end: int | None = None
    token_count: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class CheckResult(_Staged):
    passed: bool
    detail: str | None = None


class Manifest(_Staged):
    """_manifest.<table>.json, written by Validate; Index refuses anything without passed=True."""

    article_id: str
    doc_version: int
    pipeline_version: str
    embedding_model: str
    dimension: int
    chunk_count: int
    vector_count: int
    checks: dict[str, CheckResult]
    passed: bool
    created_at: str
