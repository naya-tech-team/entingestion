"""Plain text and Markdown."""

from __future__ import annotations

import re

from ingest.models.staging import Block, BlockType

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_TABLE = re.compile(r"^\s*\|.*\|\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def extract_plain(text: str) -> list[Block]:
    paragraphs = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    return [Block(type=BlockType.PARAGRAPH, text=p.strip()) for p in paragraphs if p.strip()]


def extract_markdown(text: str) -> list[Block]:
    blocks: list[Block] = []
    path: list[tuple[int, str]] = []
    buffer: list[str] = []
    kind = BlockType.PARAGRAPH
    in_code = False

    def heading_path() -> list[str]:
        return [title for _, title in path]

    def flush() -> None:
        nonlocal buffer, kind
        body = "\n".join(buffer).strip("\n")
        if body.strip():
            blocks.append(Block(type=kind, text=body, heading_path=heading_path()))
        buffer, kind = [], BlockType.PARAGRAPH

    for line in text.replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            if in_code:
                flush()
                in_code = False
            else:
                flush()
                in_code, kind = True, BlockType.CODE
            continue
        if in_code:
            buffer.append(line)
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            level, title = len(heading.group(1)), heading.group(2).strip()
            path = [(lvl, t) for lvl, t in path if lvl < level] + [(level, title)]
            blocks.append(
                Block(
                    type=BlockType.HEADING,
                    text=title,
                    heading_path=heading_path()[:-1],
                    level=level,
                )
            )
            continue
        if not line.strip():
            flush()
            continue
        line_kind = (
            BlockType.TABLE
            if _TABLE.match(line)
            else BlockType.LIST
            if _LIST.match(line)
            else BlockType.PARAGRAPH
        )
        if buffer and line_kind != kind:
            flush()
        kind = line_kind
        buffer.append(line)
    flush()
    return blocks
