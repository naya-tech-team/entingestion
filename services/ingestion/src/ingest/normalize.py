"""Text normalization (the Normalize step). Deterministic for (input, pipeline_version): no
lowercasing, stemming or stopword removal."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

import ftfy

from ingest.models.staging import Block, BlockType

_ZERO_WIDTH = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")
_SPACES = re.compile("[ \t\u00a0]+")
_PAGE_NUMBER = re.compile(r"^(?:page\s*)?\d{1,4}(?:\s*(?:/|of)\s*\d{1,4})?$", re.IGNORECASE)
_DIGITS = re.compile(r"\d+")

# A short line that repeats on at least this share of pages is a running header or footer.
_REPEAT_SHARE = 0.5
_REPEAT_MAX_LEN = 120
_MIN_PAGES_FOR_REPEATS = 3


def clean_text(text: str, keep_lines: bool = False) -> str:
    text = ftfy.fix_text(text)
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH.sub("", text)
    text = _CONTROL.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    if keep_lines:
        lines = [_SPACES.sub(" ", line).rstrip() for line in text.split("\n")]
        return "\n".join(lines).strip("\n")
    return " ".join(text.split())


def _signature(text: str) -> str:
    """Exact text, except lines that mention a page ("Report - page 3") match ignoring digits."""
    lowered = text.lower()
    return _DIGITS.sub("#", lowered) if "page" in lowered else lowered


def _edge_ids(blocks: list[Block]) -> set[int]:
    """Indexes of the first and last block of each page: where headers and footers live."""
    first: dict[int, int] = {}
    last: dict[int, int] = {}
    for i, b in enumerate(blocks):
        if b.page is not None:
            first.setdefault(b.page, i)
            last[b.page] = i
    return set(first.values()) | set(last.values())


def drop_running_headers(blocks: list[Block]) -> list[Block]:
    """Remove page numbers and running headers/footers: short lines at the top or bottom of a page
    that repeat on at least half the pages."""
    pages = {b.page for b in blocks if b.page is not None}
    blocks = [b for b in blocks if not (b.page is not None and _PAGE_NUMBER.match(b.text.strip()))]
    if len(pages) < _MIN_PAGES_FOR_REPEATS:
        return blocks
    edges = _edge_ids(blocks)
    candidates = {i for i in edges if len(blocks[i].text) <= _REPEAT_MAX_LEN}
    on_pages = Counter(
        sig for sig in {(blocks[i].page, _signature(blocks[i].text)) for i in candidates}
    )
    per_sig = Counter(sig for (_page, sig) in on_pages)
    repeated = {sig for sig, n in per_sig.items() if n / len(pages) >= _REPEAT_SHARE}
    return [
        b for i, b in enumerate(blocks) if not (i in candidates and _signature(b.text) in repeated)
    ]


def normalize_blocks(blocks: list[Block]) -> list[Block]:
    out: list[Block] = []
    for block in drop_running_headers(blocks):
        keep_lines = block.type in (BlockType.CODE, BlockType.TABLE)
        text = clean_text(block.text, keep_lines=keep_lines)
        if not text:
            continue
        path = [p for p in (clean_text(h) for h in block.heading_path) if p]
        out.append(block.model_copy(update={"text": text, "heading_path": path}))
    return out
