"""Turn (simple, well-formed) HTML into canonical blocks. Used for DOCX (via mammoth) and as the
fallback when trafilatura finds no main content."""

from __future__ import annotations

from html.parser import HTMLParser

from ingest.models.staging import Block, BlockType

_HEADINGS = {f"h{i}": i for i in range(1, 7)}
_SKIP = {"script", "style", "noscript", "template", "nav", "footer", "header", "aside"}
_BLOCK_TAGS = {"p", "li", "pre", "td", "th", "blockquote", "div", "section", "article", "dd", "dt"}


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.title: str | None = None
        self._path: list[tuple[int, str]] = []
        self._text: list[str] = []
        self._kind: BlockType | None = None
        self._level: int | None = None
        self._skip = 0
        self._in_title = False
        self._table_rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._in_table = 0

    def _heading_path(self) -> list[str]:
        return [t for _, t in self._path]

    def _flush(self) -> None:
        text = (
            " ".join("".join(self._text).split())
            if self._kind is not BlockType.CODE
            else "".join(self._text).strip("\n")
        )
        kind, level = self._kind or BlockType.PARAGRAPH, self._level
        self._text, self._kind, self._level = [], None, None
        if not text:
            return
        if kind is BlockType.HEADING and level is not None:
            self._path = [(lvl, t) for lvl, t in self._path if lvl < level] + [(level, text)]
            self.blocks.append(
                Block(type=kind, text=text, heading_path=self._heading_path()[:-1], level=level)
            )
            return
        self.blocks.append(Block(type=kind, text=text, heading_path=self._heading_path()))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "table":
            self._flush()
            self._in_table += 1
            self._table_rows = []
            return
        if self._in_table:
            if tag == "tr":
                self._row = []
            elif tag in ("td", "th"):
                self._text = []
            return
        if tag in _HEADINGS:
            self._flush()
            self._kind, self._level = BlockType.HEADING, _HEADINGS[tag]
        elif tag == "pre":
            self._flush()
            self._kind = BlockType.CODE
        elif tag == "li":
            self._flush()
            self._kind = BlockType.LIST
        elif tag in _BLOCK_TAGS:
            self._flush()
        elif tag == "br":
            self._text.append("\n" if self._kind is BlockType.CODE else " ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "title":
            self._in_title = False
            return
        if self._in_table:
            if tag in ("td", "th") and self._row is not None:
                self._row.append(" ".join("".join(self._text).split()))
                self._text = []
            elif tag == "tr" and self._row is not None:
                if any(self._row):
                    self._table_rows.append(self._row)
                self._row = None
            elif tag == "table":
                self._in_table -= 1
                if self._table_rows:
                    rows = [" | ".join(r) for r in self._table_rows]
                    self.blocks.append(
                        Block(
                            type=BlockType.TABLE,
                            text="\n".join(rows),
                            heading_path=self._heading_path(),
                        )
                    )
                self._table_rows = []
            return
        if tag in _HEADINGS or tag in _BLOCK_TAGS or tag == "pre":
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title = (self.title or "") + data.strip()
            return
        self._text.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def html_to_blocks(html: str) -> tuple[str | None, list[Block]]:
    parser = _Parser()
    parser.feed(html)
    parser.close()
    return parser.title or None, parser.blocks
