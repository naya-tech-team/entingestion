"""Recursive structure-aware chunking: section (heading path) -> block -> sentence.

- Chunks aim for `target` tokens (default 512) and never mix sections, except that consecutive
  small sibling sections (each under `min_tokens`) are merged into one chunk under their parent
  heading, with each sub-heading kept in the text.
- Consecutive chunks in a section overlap by ~`overlap` tokens of whole sentences.
- Tables and code blocks are atomic (split by lines only beyond `max_tokens`). A table split
  that way repeats its header row at the top of every continuation chunk.
- Chunks smaller than `min_tokens` are merged into the previous chunk of the same section.
- A heading with nothing under it (no text, no subsections) is a chunk of its own.
- embed_text = f"{title} \u203a {' > '.join(heading_path)}\\n\\n{text}"
  (U+203A is a single right-pointing angle quote, as in the brief).
- char_start/char_end: the chunk's span in the normalized text (NormalizedDocument.text()),
  for citations; with page_start/page_end for paged documents.

Parent-child (ChunkerConfig.parent_child, off by default): sections are cut into parents of up
to `parent_target` tokens, and each parent into children of `child_target` tokens (at least
`child_min`). Only the children are chunks (embedded and indexed); each carries its parent's id
and text, which search returns as the child's context. Parents need no table or vectors of
their own (see docs/ARCHITECTURE.md, retrieval).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ingest.chunking.tokenizer import Tokenizer
from ingest.ids import chunk_id, parent_chunk_id
from ingest.models.staging import Block, BlockType, Chunk, NormalizedDocument

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=[\"'“(\[]?[A-Z0-9])")


@dataclass
class ChunkerConfig:
    target: int = 512
    overlap: int = 64
    min_tokens: int = 64
    max_tokens: int = 2048
    parent_child: bool = False
    parent_target: int = 1024
    child_target: int = 256
    child_min: int = 128
    child_overlap: int = 32


@dataclass(eq=False)
class _Unit:
    text: str
    tokens: int
    atomic: bool
    page: int | None
    start: int | None = None  # span in the normalized text
    end: int | None = None


@dataclass
class _Draft:
    heading_path: list[str]
    units: list[_Unit] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(u.tokens for u in self.units)

    @property
    def text(self) -> str:
        return "\n\n".join(u.text for u in self.units)


@dataclass(frozen=True)
class _Placed:
    """A block and where it starts in the normalized text."""

    block: Block
    start: int | None


@dataclass
class _Section:
    path: list[str]
    # (inline sub-heading or None, blocks). A merged section has one part per small section.
    parts: list[tuple[_Placed | None, list[_Placed]]]
    tokens: int
    own_path: list[str]  # the path it started with (before merging made it its parent's)
    small: bool


def split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text) if s.strip()]


class Chunker:
    def __init__(self, tokenizer: Tokenizer, config: ChunkerConfig | None = None) -> None:
        self.tok = tokenizer
        self.cfg = config or ChunkerConfig()

    # -- units -------------------------------------------------------------

    def _units(self, placed: _Placed, limit: int) -> list[_Unit]:
        block, start = placed.block, placed.start
        tokens = self.tok.count(block.text)
        end = None if start is None else start + len(block.text)
        if block.type in (BlockType.TABLE, BlockType.CODE):
            if tokens <= self.cfg.max_tokens:
                return [_Unit(block.text, tokens, True, block.page, start, end)]
            return self._split_lines(placed)
        if tokens <= limit:
            return [_Unit(block.text, tokens, False, block.page, start, end)]
        units: list[_Unit] = []
        cursor = 0
        for sentence in split_sentences(block.text):
            parts = (
                [sentence]
                if self.tok.count(sentence) <= limit
                else self.tok.split(sentence, limit)  # a run-on "sentence": hard split by tokens
            )
            for part in parts:
                found = block.text.find(part, cursor)
                at = found if found >= 0 else cursor
                cursor = at + len(part)
                units.append(
                    _Unit(
                        part,
                        self.tok.count(part),
                        False,
                        block.page,
                        None if start is None else start + at,
                        None if start is None else start + cursor,
                    )
                )
        return units

    def _split_lines(self, placed: _Placed) -> list[_Unit]:
        """An oversized table or code block, by lines. Table continuations repeat the header."""
        block, start = placed.block, placed.start
        lines = block.text.split("\n")
        header = lines[0] if block.type is BlockType.TABLE and len(lines) > 1 else None
        # +1: the newline joining each line to the next is a token of its own (or merges into one)
        header_tokens = self.tok.count(header) + 1 if header is not None else 0
        units: list[_Unit] = []
        group: list[str] = []
        size = 0
        group_at = at = 0

        def emit() -> None:
            body = "\n".join(group)
            text = f"{header}\n{body}" if header is not None and units else body
            s = None if start is None else start + group_at
            e = None if start is None else start + group_at + len(body)
            units.append(_Unit(text, self.tok.count(text), True, block.page, s, e))

        for line in lines:
            n = self.tok.count(line) + 1
            budget = self.cfg.max_tokens - (header_tokens if units else 0)
            if group and size + n > budget:
                emit()
                group, size, group_at = [], 0, at
            group.append(line)
            size += n
            at += len(line) + 1
        if group:
            emit()
        return units

    # -- packing -------------------------------------------------------------

    def _overlap(self, draft: _Draft, overlap: int) -> list[_Unit]:
        """Trailing whole sentences of the previous chunk, up to `overlap` tokens."""
        carried: list[_Unit] = []
        size = 0
        for unit in reversed(draft.units):
            if unit.atomic or size + unit.tokens > overlap:
                break
            carried.insert(0, unit)
            size += unit.tokens
        return carried

    def _pack(
        self, path: list[str], units: list[_Unit], target: int, overlap: int, min_tokens: int
    ) -> list[_Draft]:
        drafts: list[_Draft] = []
        current = _Draft(path)
        fresh = 0  # units added since the overlap
        for unit in units:
            if fresh and current.tokens + unit.tokens > target:
                drafts.append(current)
                current = _Draft(path, self._overlap(current, overlap))
                fresh = 0
            current.units.append(unit)
            fresh += 1
        if fresh:
            drafts.append(current)
        # Merge undersized chunks into their predecessor in the same section.
        merged: list[_Draft] = []
        for draft in drafts:
            if merged and draft.tokens < min_tokens:
                tail = [u for u in draft.units if all(u is not m for m in merged[-1].units)]
                merged[-1].units.extend(tail)
            else:
                merged.append(draft)
        return merged

    # -- sections ----------------------------------------------------------------

    def _sections(self, doc: NormalizedDocument) -> list[_Section]:
        """Blocks grouped by heading path, in order. Headings live in heading_path/embed_text,
        except a heading with nothing under it, which becomes a section of its own text."""
        offsets = doc.block_offsets()
        sections: list[_Section] = []
        headings: dict[tuple[str, ...], _Placed] = {}  # the heading that opened each path
        blocks = doc.blocks
        for i, block in enumerate(blocks):
            placed = _Placed(block, offsets[i])
            if block.type is BlockType.HEADING:
                path = [*block.heading_path, block.text]
                headings[tuple(path)] = placed
                if self._heading_only(blocks, i):  # its text is the heading, kept inline
                    tokens = self.tok.count(block.text)
                    small = tokens < self.cfg.min_tokens
                    sections.append(_Section(path, [(placed, [])], tokens, path, small))
                continue
            if not sections or sections[-1].own_path != block.heading_path:
                path = block.heading_path
                sections.append(_Section(path, [(None, [])], 0, path, True))
            section = sections[-1]
            section.parts[-1][1].append(placed)
            section.tokens += self.tok.count(block.text)
            section.small = section.tokens < self.cfg.min_tokens
        return self._merge_small(sections, headings)

    @staticmethod
    def _heading_only(blocks: list[Block], i: int) -> bool:
        level = blocks[i].level or 1
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        return nxt is None or (nxt.type is BlockType.HEADING and (nxt.level or 1) <= level)

    def _merge_small(
        self, sections: list[_Section], headings: dict[tuple[str, ...], _Placed]
    ) -> list[_Section]:
        """Consecutive small sections under one parent heading become one section of the parent,
        each keeping its own heading inline (an FAQ of short answers is one chunk, not ten)."""

        def heading(path: list[str]) -> _Placed:
            return headings.get(tuple(path)) or _Placed(
                Block(type=BlockType.PARAGRAPH, text=path[-1]), None
            )

        out: list[_Section] = []
        for s in sections:
            prev = out[-1] if out else None
            fits = (
                prev is not None
                and prev.small
                and s.small
                and len(s.path) > 0
                and prev.tokens + s.tokens <= self.cfg.target
            )
            if prev is not None and fits:
                parent = s.path[:-1]
                if prev.path == parent:  # a child of the group's heading
                    prev.parts.append((s.parts[0][0] or heading(s.path), s.parts[0][1]))
                    prev.tokens += s.tokens
                    continue
                if prev.path == prev.own_path and prev.path and prev.path[:-1] == parent:
                    # A sibling: the group moves up to the parent, both keep their headings.
                    own, first = prev.parts[0]
                    prev.parts[0] = (own or heading(prev.path), first)
                    prev.path = parent
                    prev.parts.append((s.parts[0][0] or heading(s.path), s.parts[0][1]))
                    prev.tokens += s.tokens
                    continue
            out.append(s)
        return out

    def _section_units(self, section: _Section, limit: int) -> list[_Unit]:
        units: list[_Unit] = []
        for inline, placed in section.parts:
            # A merged section's sub-headings, and a heading-only section's heading, are text.
            if inline is not None:
                text = inline.block.text
                end = None if inline.start is None else inline.start + len(text)
                units.append(
                    _Unit(text, self.tok.count(text), False, inline.block.page, inline.start, end)
                )
            for p in placed:
                units += self._units(p, limit)
        return units

    # -- public ----------------------------------------------------------------

    def chunk(self, doc: NormalizedDocument) -> list[Chunk]:
        cfg = self.cfg
        chunks: list[Chunk] = []
        parents = 0
        for section in self._sections(doc):
            path = section.path
            if not cfg.parent_child:
                drafts = self._pack(
                    path, self._section_units(section, cfg.target), cfg.target, cfg.overlap,
                    cfg.min_tokens,
                )  # fmt: skip
                chunks += [self._chunk(doc, len(chunks) + i, d) for i, d in enumerate(drafts)]
                continue
            units = self._section_units(section, cfg.child_target)
            for parent in self._pack(path, units, cfg.parent_target, 0, cfg.child_min):
                pid = parent_chunk_id(doc.article_id, doc.doc_version, parents)
                parents += 1
                children = self._pack(
                    path, parent.units, cfg.child_target, cfg.child_overlap, cfg.child_min
                )
                for child in children:
                    chunks.append(self._chunk(doc, len(chunks), child, pid, parent.text))
        return chunks

    def _chunk(
        self,
        doc: NormalizedDocument,
        ordinal: int,
        draft: _Draft,
        parent_id: str | None = None,
        parent_text: str | None = None,
    ) -> Chunk:
        text = draft.text
        pages = [u.page for u in draft.units if u.page is not None]
        starts = [u.start for u in draft.units if u.start is not None]
        ends = [u.end for u in draft.units if u.end is not None]
        return Chunk(
            chunk_id=chunk_id(doc.article_id, doc.doc_version, ordinal),
            article_id=doc.article_id,
            doc_version=doc.doc_version,
            ordinal=ordinal,
            parent_id=parent_id,
            parent_text=parent_text if parent_text != text else None,
            text=text,
            embed_text=embed_text(doc.title, draft.heading_path, text),
            heading_path=draft.heading_path,
            page_start=min(pages) if pages else None,
            page_end=max(pages) if pages else None,
            char_start=min(starts) if starts else None,
            char_end=max(ends) if ends else None,
            token_count=self.tok.count(text),
            metadata=_chunk_metadata(doc),
        )


def embed_text(title: str | None, heading_path: list[str], text: str) -> str:
    prefix_parts = [p for p in (title, " > ".join(heading_path)) if p]
    prefix = " \u203a ".join(prefix_parts)  # U+203A, as in brief §4.4
    return f"{prefix}\n\n{text}" if prefix else text


def _chunk_metadata(doc: NormalizedDocument) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "source": doc.source,
        "process_id": doc.process_id,
        "tenant_id": doc.tenant_id,
        "published_at": doc.published_at,
        "title": doc.title,
    }
    if doc.metadata:
        meta["article"] = doc.metadata
    return {k: v for k, v in meta.items() if v is not None}
