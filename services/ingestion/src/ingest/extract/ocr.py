"""Textract document analysis output (LAYOUT + TABLES) -> canonical blocks.

Textract (StartDocumentAnalysis with an OutputConfig) writes its result as numbered JSON files
under <prefix>/<JobId>/. Each holds a slice of Blocks; relationships may point into another
file, so every file is read before blocks are built.

With the LAYOUT feature, LAYOUT_* blocks come in reading order (columns resolved) and point at
their LINEs:

    LAYOUT_TITLE, LAYOUT_SECTION_HEADER  -> HEADING (level 1 and 2)
    LAYOUT_TEXT, LAYOUT_KEY_VALUE        -> PARAGRAPH
    LAYOUT_LIST                          -> one LIST block per item (its LAYOUT_TEXT children)
    LAYOUT_TABLE                         -> the TABLE (TABLES feature) it covers, rows from CELLs
    LAYOUT_HEADER, _FOOTER, _PAGE_NUMBER -> dropped (running headers and page numbers)
    LAYOUT_FIGURE                        -> dropped (labels inside charts and images)

Output without LAYOUT blocks (text detection, or an older job) falls back to LINEs: per page,
two-column pages are read column by column, and lines are grouped into paragraphs the way the
PDF extractor does (a vertical gap of more than 1.5 line heights starts a new paragraph).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ingest.models.staging import Block, BlockType

_PARAGRAPH_GAP = 1.5
_HEADINGS = {"LAYOUT_TITLE": 1, "LAYOUT_SECTION_HEADER": 2}
_TEXT = frozenset({"LAYOUT_TEXT", "LAYOUT_KEY_VALUE"})
# Two-column detection: enough lines entirely on each side of the page's middle.
_MIDDLE = 0.5
_MIN_COLUMN_LINES = 3
_SPANNING_SHARE = 0.25


@dataclass(frozen=True)
class _Box:
    left: float
    top: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def bottom(self) -> float:
        return self.top + self.height

    def overlap(self, other: _Box) -> float:
        w = min(self.right, other.right) - max(self.left, other.left)
        h = min(self.bottom, other.bottom) - max(self.top, other.top)
        return max(w, 0.0) * max(h, 0.0)


def _box(block: dict[str, Any]) -> _Box:
    b = (block.get("Geometry") or {}).get("BoundingBox") or {}
    return _Box(
        float(b.get("Left", 0.0)),
        float(b.get("Top", 0.0)),
        float(b.get("Width", 0.0)),
        float(b.get("Height", 0.0)),
    )


class _Doc:
    """Every block by id, across the output files."""

    def __init__(self, parts: Iterable[dict[str, Any]]) -> None:
        self.blocks: list[dict[str, Any]] = []
        self.pages = 0
        for part in parts:
            self.pages = max(
                self.pages, int((part.get("DocumentMetadata") or {}).get("Pages") or 0)
            )
            self.blocks += part.get("Blocks") or []
        self.by_id = {str(b["Id"]): b for b in self.blocks if "Id" in b}

    def children(self, block: dict[str, Any], kind: str | None = None) -> list[dict[str, Any]]:
        ids = [
            i
            for rel in block.get("Relationships") or []
            if rel.get("Type") == "CHILD"
            for i in rel.get("Ids") or []
        ]
        found = [self.by_id[i] for i in ids if i in self.by_id]
        return [b for b in found if kind is None or b.get("BlockType") == kind]

    def text(self, block: dict[str, Any]) -> str:
        """A block's text: its own, or its LINE (or WORD) children's, one line per LINE."""
        if block.get("BlockType") in ("LINE", "WORD"):
            return str(block.get("Text") or "").strip()
        lines = [self.text(c) for c in self.children(block, "LINE")]
        if not lines:
            lines = [" ".join(self.text(c) for c in self.children(block, "WORD"))]
        return "\n".join(t for t in lines if t)


def blocks_from_textract(parts: Iterable[dict[str, Any]]) -> tuple[list[Block], int]:
    """(blocks in reading order, page count) from Textract's output files."""
    doc = _Doc(parts)
    layout = _layout_in_reading_order(doc)
    blocks = _from_layout(doc, layout) if layout else _from_lines(doc)
    pages = max([doc.pages] + [b.page or 0 for b in blocks])
    return blocks, pages


def title_of(blocks: list[Block]) -> str | None:
    """The first top-level heading (LAYOUT_TITLE), if any."""
    return next((b.text for b in blocks if b.type is BlockType.HEADING and b.level == 1), None)


# -- LAYOUT ------------------------------------------------------------------------------------


def _is_layout(block: dict[str, Any]) -> bool:
    return str(block.get("BlockType", "")).startswith("LAYOUT_")


def _layout_in_reading_order(doc: _Doc) -> list[dict[str, Any]]:
    """Each PAGE lists its LAYOUT blocks in reading order; any it doesn't list follow in output
    order on their page."""
    ordered: list[dict[str, Any]] = []
    for page in sorted(
        (b for b in doc.blocks if b.get("BlockType") == "PAGE"),
        key=lambda b: int(b.get("Page") or 1),
    ):
        ordered += [c for c in doc.children(page) if _is_layout(c)]
    listed = {id(b) for b in ordered}
    rest = [b for b in doc.blocks if _is_layout(b) and id(b) not in listed]
    merged = ordered + rest
    return sorted(merged, key=lambda b: int(b.get("Page") or 1))  # stable within a page


def _from_layout(doc: _Doc, layout: list[dict[str, Any]]) -> list[Block]:
    tables: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for b in doc.blocks:
        if b.get("BlockType") == "TABLE":
            tables[int(b.get("Page") or 1)].append(b)
    # A LAYOUT_TEXT inside a LAYOUT_LIST is read with its list, not again on its own.
    in_list = {
        str(c["Id"]) for b in layout if b["BlockType"] == "LAYOUT_LIST" for c in doc.children(b)
    }

    out: list[Block] = []
    path: list[tuple[int, str]] = []
    used_tables: set[str] = set()

    def heading_path() -> list[str]:
        return [t for _, t in path]

    def add(kind: BlockType, text: str, page: int) -> None:
        if text.strip():
            out.append(Block(type=kind, text=text, heading_path=heading_path(), page=page))

    for block in layout:
        kind, page = str(block["BlockType"]), int(block.get("Page") or 1)
        if str(block.get("Id")) in in_list:
            continue
        if kind in _HEADINGS:
            text = " ".join(doc.text(block).split())
            if not text:
                continue
            level = _HEADINGS[kind]
            path = [(lvl, t) for lvl, t in path if lvl < level]
            out.append(
                Block(
                    type=BlockType.HEADING,
                    text=text,
                    heading_path=heading_path(),
                    page=page,
                    level=level,
                )
            )
            path.append((level, text))
        elif kind in _TEXT:
            add(BlockType.PARAGRAPH, doc.text(block), page)
        elif kind == "LAYOUT_LIST":
            items = doc.children(block, "LAYOUT_TEXT") or [block]
            for item in items:
                add(BlockType.LIST, " ".join(doc.text(item).split()), page)
        elif kind == "LAYOUT_TABLE":
            table = _covering_table(block, tables[page], used_tables)
            if table is not None:
                used_tables.add(str(table["Id"]))
                add(BlockType.TABLE, _table_text(doc, table), page)
            else:  # TABLES not requested, or nothing matched: keep the text
                add(BlockType.PARAGRAPH, doc.text(block), page)
    return out


def _covering_table(
    layout: dict[str, Any], candidates: list[dict[str, Any]], used: set[str]
) -> dict[str, Any] | None:
    area = _box(layout)
    best = max(
        (t for t in candidates if str(t["Id"]) not in used),
        key=lambda t: area.overlap(_box(t)),
        default=None,
    )
    return best if best is not None and area.overlap(_box(best)) > 0 else None


def _table_text(doc: _Doc, table: dict[str, Any]) -> str:
    """Rows of ' | '-joined cells, like the other extractors' tables."""
    cells: dict[tuple[int, int], str] = {}
    for cell in doc.children(table, "CELL"):
        words = [doc.text(w) for w in doc.children(cell, "WORD")]
        cells[(int(cell.get("RowIndex") or 1), int(cell.get("ColumnIndex") or 1))] = " ".join(
            w for w in words if w
        )
    if not cells:
        return doc.text(table)
    rows = max(r for r, _ in cells)
    cols = max(c for _, c in cells)
    lines = [
        " | ".join(cells.get((r, c), "") for c in range(1, cols + 1)) for r in range(1, rows + 1)
    ]
    return "\n".join(line for line in lines if line.strip(" |"))


# -- LINE fallback -----------------------------------------------------------------------------


def _from_lines(doc: _Doc) -> list[Block]:
    lines: dict[int, list[tuple[_Box, str]]] = defaultdict(list)
    for block in doc.blocks:
        if block.get("BlockType") != "LINE" or not str(block.get("Text", "")).strip():
            continue
        lines[int(block.get("Page") or 1)].append((_box(block), str(block["Text"])))
    blocks: list[Block] = []
    for page in sorted(lines):
        blocks.extend(_paragraphs(_reading_order(lines[page]), page))
    return blocks


def _reading_order(lines: list[tuple[_Box, str]]) -> list[tuple[_Box, str]]:
    """Top to bottom; on a two-column page the left column first. Lines spanning both columns
    (titles) count as the left column."""
    left = [ln for ln in lines if ln[0].right <= _MIDDLE + 0.02]
    right = [ln for ln in lines if ln[0].left >= _MIDDLE - 0.02]
    spanning = len(lines) - len(left) - len(right)
    two_columns = (
        len(left) >= _MIN_COLUMN_LINES
        and len(right) >= _MIN_COLUMN_LINES
        and spanning <= _SPANNING_SHARE * len(lines)
    )
    if not two_columns:
        return sorted(lines, key=lambda ln: (ln[0].top, ln[0].left))
    return sorted(lines, key=lambda ln: (ln[0].left >= _MIDDLE - 0.02, ln[0].top, ln[0].left))


def _paragraphs(lines: list[tuple[_Box, str]], page: int) -> list[Block]:
    out: list[Block] = []
    paragraph: list[str] = []
    last: _Box | None = None
    for box, text in lines:
        gap_limit = _PARAGRAPH_GAP * max(box.height, 1e-4)
        new_column = last is not None and box.top < last.top
        if paragraph and last is not None and (new_column or box.top - last.bottom > gap_limit):
            out.append(Block(type=BlockType.PARAGRAPH, text="\n".join(paragraph), page=page))
            paragraph = []
        paragraph.append(text.strip())
        last = box
    if paragraph:
        out.append(Block(type=BlockType.PARAGRAPH, text="\n".join(paragraph), page=page))
    return out
