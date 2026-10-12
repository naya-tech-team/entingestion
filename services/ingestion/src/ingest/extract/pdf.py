"""Text PDFs via pdfplumber (MIT; pdfminer.six under the hood).

Lines are grouped into paragraph blocks by vertical spacing, so running headers and footers stay
separate blocks that Normalize can drop. Tables found by pdfplumber become atomic table blocks,
and their text is excluded from the paragraphs.

Headings come from the PDF outline (bookmarks) when it has one, and from the fonts: a line whose
dominant font size is clearly above the body text's median size is a heading (the larger, the
higher its level), and so is a short line set entirely in bold. Lines that repeat on most pages
(running headers) never count as headings. Paragraphs carry the heading path above them, as in
HTML, and the document title is the PDF's Title metadata or else the first top-level heading.

Scanned or textless PDFs produce no blocks; Extract then routes them to OCR (steps/ocr.py).
"""

from __future__ import annotations

import io
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Any

import pdfplumber
from pdfminer.pdfdocument import PDFEncryptionError, PDFNoOutlines, PDFPasswordIncorrect
from pdfplumber.utils.exceptions import PdfminerException

from ingest.errors import ErrorCode, make_error
from ingest.models.staging import Block, BlockType

# A gap larger than this many line heights starts a new paragraph.
_PARAGRAPH_GAP = 1.5
# A heading's font is at least this much larger than the body text's median (ratio and points).
_HEADING_RATIO = 1.15
_HEADING_MIN_PT = 1.0
# A bold line is a heading only if it's short and doesn't read like a sentence.
_BOLD_MAX_CHARS = 80
_BOLD_MAX_WORDS = 12
_BOLD_SHARE = 0.8
_SENTENCE_END = (".", "!", "?", ";", ",")
_MAX_LEVEL = 6
# A line on at least this share of pages (of 3 or more) is a running header, not a heading.
_REPEAT_SHARE = 0.5
_MIN_PAGES_FOR_REPEATS = 3
_BOLD_FONT = re.compile(r"bold|black|heavy|semibold|demi", re.IGNORECASE)
_DIGITS = re.compile(r"\d+")


@dataclass
class _Line:
    page: int
    top: float
    bottom: float
    text: str
    size: float  # dominant font size
    bold: bool
    level: int | None = None  # heading level, once classified


def extract(data: bytes) -> tuple[str | None, list[Block], int]:
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except PdfminerException as exc:
        # pdfplumber wraps pdfminer's errors; the original is the first argument.
        cause = exc.args[0] if exc.args else exc
        if isinstance(cause, PDFPasswordIncorrect | PDFEncryptionError):
            raise make_error(ErrorCode.UNREADABLE, "PDF is encrypted") from exc
        raise make_error(ErrorCode.UNREADABLE, f"PDF: {cause!r}") from exc
    with pdf:
        title = str((pdf.metadata or {}).get("Title") or "").strip() or None
        tables: list[tuple[int, float, Block]] = []
        lines: list[_Line] = []
        for number, page in enumerate(pdf.pages, start=1):
            page_tables, page_lines = _page_items(page, number)
            tables += page_tables
            lines += page_lines
        _classify(lines, _outline(pdf), len(pdf.pages))
        blocks = _assemble(lines, tables)
        if title is None:
            title = next((b.text for b in blocks if b.type is BlockType.HEADING), None)
        return title, blocks, len(pdf.pages)


def _outline(pdf: Any) -> dict[str, int]:
    """Bookmark titles (normalized) -> depth, 1 = top level. Empty without an outline."""
    try:
        entries = list(pdf.doc.get_outlines())
    except PDFNoOutlines:
        return {}
    except Exception:  # a broken outline isn't worth failing the document for
        return {}
    return {_key(str(title)): min(int(level), _MAX_LEVEL) for level, title, *_ in entries if title}


def _key(text: str) -> str:
    return " ".join(text.lower().split())


def _page_items(page: Any, number: int) -> tuple[list[tuple[int, float, Block]], list[_Line]]:
    """The page's tables (with their top) and its text lines outside them."""
    found = page.find_tables()
    tables = []
    for table in found:
        rows = [" | ".join((cell or "").strip() for cell in row) for row in table.extract()]
        rows = [r for r in rows if r.strip(" |")]
        if rows:
            block = Block(type=BlockType.TABLE, text="\n".join(rows), page=number)
            tables.append((number, float(table.bbox[1]), block))
    text_page = page
    for table in found:  # keep table text out of the paragraphs
        text_page = text_page.outside_bbox(table.bbox)
    lines = []
    for raw in text_page.extract_text_lines(strip=True, return_chars=True):
        chars = [c for c in raw.get("chars") or [] if str(c.get("text", "")).strip()]
        sizes = Counter(round(float(c.get("size") or 0.0), 1) for c in chars)
        bold = sum(1 for c in chars if _BOLD_FONT.search(str(c.get("fontname") or "")))
        lines.append(
            _Line(
                page=number,
                top=float(raw["top"]),
                bottom=float(raw["bottom"]),
                text=str(raw["text"]),
                size=sizes.most_common(1)[0][0] if sizes else 0.0,
                bold=bool(chars) and bold / len(chars) >= _BOLD_SHARE,
            )
        )
    return tables, lines


def _repeated(lines: list[_Line], pages: int) -> set[str]:
    """Text (digits ignored) on at least half the pages: running headers and footers."""
    if pages < _MIN_PAGES_FOR_REPEATS:
        return set()
    on_pages = {(ln.page, _DIGITS.sub("#", _key(ln.text))) for ln in lines}
    per_text = Counter(text for _page, text in on_pages)
    return {text for text, n in per_text.items() if n / pages >= _REPEAT_SHARE}


def _classify(lines: list[_Line], outline: dict[str, int], pages: int) -> None:
    """Set .level on the lines that are headings."""
    sized = [ln for ln in lines if ln.size > 0]
    if not sized:
        return
    body = statistics.median(ln.size for ln in sized for _ in ln.text)  # by characters
    repeated = _repeated(lines, pages)

    def larger(ln: _Line) -> bool:
        return ln.size >= body * _HEADING_RATIO and ln.size - body >= _HEADING_MIN_PT

    candidates = [
        ln
        for ln in lines
        if not _DIGITS.fullmatch(ln.text.strip())
        and _DIGITS.sub("#", _key(ln.text)) not in repeated
    ]
    sizes = sorted({ln.size for ln in candidates if larger(ln)}, reverse=True)
    for ln in candidates:
        if _key(ln.text) in outline:
            ln.level = outline[_key(ln.text)]
        elif larger(ln):
            ln.level = min(sizes.index(ln.size) + 1, _MAX_LEVEL)
        elif (
            ln.bold
            and len(ln.text) <= _BOLD_MAX_CHARS
            and len(ln.text.split()) <= _BOLD_MAX_WORDS
            and not ln.text.rstrip().endswith(_SENTENCE_END)
        ):
            ln.level = min(len(sizes) + 1, _MAX_LEVEL)


def _assemble(lines: list[_Line], tables: list[tuple[int, float, Block]]) -> list[Block]:
    """Headings, paragraphs and tables in reading order, with heading paths."""
    items: list[tuple[int, float, int, Any]] = [(p, top, 1, b) for p, top, b in tables]
    items += [(ln.page, ln.top, 0, ln) for ln in lines]
    items.sort(key=lambda i: (i[0], i[1], i[2]))

    blocks: list[Block] = []
    path: list[tuple[int, str]] = []
    paragraph: list[_Line] = []
    heading: list[_Line] = []

    def flush() -> None:
        nonlocal path
        if heading:
            level = heading[0].level or 1
            text = " ".join(ln.text for ln in heading)
            path = [(lvl, t) for lvl, t in path if lvl < level]
            blocks.append(
                Block(
                    type=BlockType.HEADING,
                    text=text,
                    heading_path=[t for _, t in path],
                    page=heading[0].page,
                    level=level,
                )
            )
            path.append((level, text))
            heading.clear()
        if paragraph:
            blocks.append(
                Block(
                    type=BlockType.PARAGRAPH,
                    text="\n".join(ln.text for ln in paragraph),
                    heading_path=[t for _, t in path],
                    page=paragraph[0].page,
                )
            )
            paragraph.clear()

    def gap(previous: _Line, line: _Line) -> bool:
        height = max(line.bottom - line.top, 1.0)
        return line.page != previous.page or line.top - previous.bottom > _PARAGRAPH_GAP * height

    for _page, _top, _kind, item in items:
        if isinstance(item, Block):
            flush()
            blocks.append(item.model_copy(update={"heading_path": [t for _, t in path]}))
            continue
        line: _Line = item
        if line.level is not None:
            # A heading wrapped over several lines stays one heading.
            continues = (
                bool(heading) and heading[-1].level == line.level and not gap(heading[-1], line)
            )
            if not continues:
                flush()
            heading.append(line)
            continue
        if heading or (paragraph and gap(paragraph[-1], line)):
            flush()
        paragraph.append(line)
    flush()
    return blocks
