"""HTML via trafilatura (main-content extraction), falling back to a plain HTML walk."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import trafilatura

from ingest.extract.htmlblocks import html_to_blocks
from ingest.extract.text import decode
from ingest.models.staging import Block, BlockType


def extract(data: bytes) -> tuple[str | None, list[Block]]:
    html = decode(data)
    meta = trafilatura.extract_metadata(html)
    title = meta.title if meta and meta.title else None
    xml = trafilatura.extract(
        html,
        output_format="xml",
        include_tables=True,
        include_comments=False,
        include_formatting=False,
        favor_recall=True,
    )
    blocks = _xml_to_blocks(xml) if xml else []
    if not blocks:
        fallback_title, blocks = html_to_blocks(html)
        title = title or fallback_title
    return title, blocks


def _text(element: ET.Element) -> str:
    return " ".join("".join(element.itertext()).split())


# Elements that become blocks. What they contain belongs to them and is never walked again:
# a <p> inside a table <cell> or a list <item> is part of that table or item, not a paragraph.
_BLOCKS = frozenset({"head", "p", "list", "code", "table"})
_IGNORED = frozenset({"comments", "graphic", "ref", "lb"})


def _xml_to_blocks(xml: str) -> list[Block]:
    root = ET.fromstring(xml)  # noqa: S314 - trafilatura output, not untrusted XML
    main = root.find("main")
    body = main if main is not None else root
    blocks: list[Block] = []
    path: list[tuple[int, str]] = []

    def heading_path() -> list[str]:
        return [t for _, t in path]

    def add(kind: BlockType, text: str) -> None:
        if text.strip():
            blocks.append(Block(type=kind, text=text, heading_path=heading_path()))

    def heading(el: ET.Element) -> None:
        nonlocal path
        text = _text(el)
        if not text:
            return
        rend = (el.get("rend") or "h2").lower()
        level = int(rend[1]) if len(rend) == 2 and rend[0] == "h" and rend[1].isdigit() else 2  # noqa: PLR2004
        path = [(lvl, t) for lvl, t in path if lvl < level] + [(level, text)]
        blocks.append(
            Block(type=BlockType.HEADING, text=text, heading_path=heading_path()[:-1], level=level)
        )

    def items(el: ET.Element) -> None:
        """One LIST block per item with the item's own text; a nested list's items follow as
        blocks of their own (the item's whole text would repeat them)."""
        for item in el.findall("item"):
            own = [item.text or ""]
            for child in item:
                if child.tag != "list":
                    own.append("".join(child.itertext()))
                own.append(child.tail or "")
            add(BlockType.LIST, " ".join("".join(own).split()))
            for nested in item.findall("list"):
                items(nested)

    def walk(el: ET.Element) -> None:
        """Top-level children in document order; containers (quote, div, ...) are opened."""
        for child in el:
            if child.tag == "head":
                heading(child)
            elif child.tag == "p":
                add(BlockType.PARAGRAPH, _text(child))
            elif child.tag == "list":
                items(child)
            elif child.tag == "code":
                text = "".join(child.itertext()).strip("\n")
                if text.strip():
                    blocks.append(
                        Block(type=BlockType.CODE, text=text, heading_path=heading_path())
                    )
            elif child.tag == "table":
                rows = [" | ".join(_text(c) for c in row) for row in child.iter("row")]
                add(BlockType.TABLE, "\n".join(r for r in rows if r.strip(" |")))
            elif child.tag in _IGNORED:
                continue
            elif any(d.tag in _BLOCKS for d in child.iter() if d is not child):
                walk(child)
            else:
                add(BlockType.PARAGRAPH, _text(child))  # a container with text only

    walk(body)
    return blocks
