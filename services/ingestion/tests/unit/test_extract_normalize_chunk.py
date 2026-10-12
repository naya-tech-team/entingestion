from __future__ import annotations

import io
import zipfile

import pytest
from pypdf import PdfReader, PdfWriter

from ingest.chunking.chunker import Chunker, ChunkerConfig, embed_text
from ingest.chunking.tokenizer import Tokenizer
from ingest.errors import IngestPermanent
from ingest.extract.dispatch import extract
from ingest.extract.sniff import Kind, sniff
from ingest.models.staging import Block, BlockType, NormalizedDocument
from ingest.normalize import clean_text, drop_running_headers, normalize_blocks
from ingest.timeutil import iso_date

HTML = b"""<!doctype html><html><head><title>Guide</title></head><body>
<nav>menu</nav>
<article><h1>Guide</h1><p>Intro paragraph with enough words to count as content for extraction.</p>
<h2>Setup</h2><p>Install the tool and configure it carefully before you start using it daily.</p>
<ul><li>First item</li><li>Second item</li></ul>
<table><tr><th>Key</th><th>Value</th></tr><tr><td>a</td><td>1</td></tr></table>
</article></body></html>"""


OOXML = "http://schemas.openxmlformats.org"
RELS_TYPE = "application/vnd.openxmlformats-package.relationships+xml"
CONTENT_TYPES = (
    f'<?xml version="1.0"?><Types xmlns="{OOXML}/package/2006/content-types">'
    f'<Default Extension="rels" ContentType="{RELS_TYPE}"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
)
RELS = (
    f'<?xml version="1.0"?><Relationships xmlns="{OOXML}/package/2006/relationships">'
    f'<Relationship Id="rId1" Type="{OOXML}/officeDocument/2006/relationships/officeDocument"'
    ' Target="word/document.xml"/></Relationships>'
)
DOCUMENT = (
    f'<?xml version="1.0"?><w:document xmlns:w="{OOXML}/wordprocessingml/2006/main"><w:body>'
    '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Report</w:t></w:r></w:p>'
    "<w:p><w:r><w:t>First paragraph of the report.</w:t></w:r></w:p></w:body></w:document>"
)


OOXML = "http://schemas.openxmlformats.org"
RELS_TYPE = "application/vnd.openxmlformats-package.relationships+xml"
CONTENT_TYPES = (
    f'<?xml version="1.0"?><Types xmlns="{OOXML}/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
)
RELS = (
    f'<?xml version="1.0"?><Relationships xmlns="{OOXML}/package/2006/relationships">'
    f'<Relationship Id="rId1" Type="{OOXML}/officeDocument/2006/relationships/officeDocument"'
    ' Target="word/document.xml"/></Relationships>'
)
DOCUMENT = (
    f'<?xml version="1.0"?><w:document xmlns:w="{OOXML}/wordprocessingml/2006/main"><w:body>'
    '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Report</w:t></w:r></w:p>'
    "<w:p><w:r><w:t>First paragraph of the report.</w:t></w:r></w:p></w:body></w:document>"
)


def docx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", DOCUMENT)
    return buf.getvalue()


PdfLine = tuple[float, float, str] | tuple[float, float, str, float, bool]


def make_pdf(pages: list[list[PdfLine]]) -> bytes:
    """A minimal text PDF: each page is a list of (x, y_from_top, text) lines in Helvetica 12,
    or (x, y_from_top, text, size, bold)."""
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]  # pages tree filled below
    kids = []
    for lines in pages:
        styled = [(*line, 12, False)[:5] for line in lines]
        content = "".join(
            f"BT /F{2 if bold else 1} {size:.0f} Tf {x:.0f} {842 - y:.0f} Td ({text}) Tj ET\n"
            for x, y, text, size, bold in styled
        ).encode()
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        content_ref = len(objects)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents %d 0 R"
            b" /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
            b" /F2 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >> >> >> >>"
            % content_ref
        )
        kids.append(len(objects))
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids),
        len(kids),
    )
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def pdf_bytes(pages: int = 3) -> bytes:
    """Pages with a running header, a body line, and a page number at the bottom."""
    return make_pdf(
        [
            [
                (72, 40, "ACME Corp Confidential"),
                (72, 100, f"Body text for section {n} talks about topic {n}."),
                (300, 800, str(n)),
            ]
            for n in range(1, pages + 1)
        ]
    )


def encrypted_pdf() -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf([[(72, 72, "secret")]]))))
    writer.encrypt(user_password="pw", owner_password="pw", algorithm="AES-256")  # noqa: S106 - fixture
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# -- sniff + extract -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "name", "kind"),
    [
        (b"%PDF-1.7 ...", "x.bin", Kind.PDF),
        (HTML, "page", Kind.HTML),
        (b"# Title\n\ntext", "notes.md", Kind.MARKDOWN),
        (b"plain words", "a.txt", Kind.TEXT),
        (b"\x00\x01\x02", "blob", Kind.UNKNOWN),
    ],
)
def test_sniff(data: bytes, name: str, kind: Kind) -> None:
    assert sniff(data, name) is kind


def test_html_blocks_inside_tables_and_lists_are_not_repeated() -> None:
    """Review repro: a <p> inside a table cell was a TABLE block and a PARAGRAPH again."""
    html = b"""<html><body><article><h1>Plans</h1>
    <p>We offer several plans for teams of all sizes and you can switch plans at any time.</p>
    <table><tr><th>Plan</th><th>Price</th></tr><tr><td><p>Basic plan</p></td><td>10</td></tr>
    </table>
    <ul><li>Outer one<ul><li>Inner A</li><li>Inner B</li></ul></li><li>Outer two</li></ul>
    <blockquote><p>A quoted customer who loves the product and says so at length.</p></blockquote>
    </article></body></html>"""
    _, blocks, _, _ = extract(Kind.HTML, html)

    texts = [b.text for b in blocks]
    assert [b.type for b in blocks if "Basic plan" in b.text] == [BlockType.TABLE]
    assert [(b.type, b.text) for b in blocks if b.type is BlockType.LIST] == [
        (BlockType.LIST, "Outer one"),
        (BlockType.LIST, "Inner A"),
        (BlockType.LIST, "Inner B"),
        (BlockType.LIST, "Outer two"),
    ]
    assert sum("quoted customer" in t for t in texts) == 1  # a container's paragraph, once
    assert len(texts) == len(set(texts))


def test_html_extraction_keeps_structure() -> None:
    title, blocks, _, extractor = extract(Kind.HTML, HTML)
    assert title == "Guide"
    assert extractor == "trafilatura"
    texts = [b.text for b in blocks]
    assert any("Install the tool" in t for t in texts)
    assert not any(t == "menu" for t in texts)  # boilerplate dropped
    setup = next(b for b in blocks if "Install the tool" in b.text)
    assert setup.heading_path[-1] == "Setup"


def test_markdown_headings_lists_code() -> None:
    md = b"# A\n\nIntro.\n\n## B\n\n- one\n- two\n\n```\ncode()\n```\n"
    _, blocks, _, _ = extract(Kind.MARKDOWN, md)
    kinds = [(b.type, b.heading_path) for b in blocks]
    assert (BlockType.LIST, ["A", "B"]) in kinds
    assert (BlockType.CODE, ["A", "B"]) in kinds


def test_docx_extraction() -> None:
    title, blocks, _, extractor = extract(Kind.DOCX, docx_bytes())
    assert extractor == "mammoth"
    assert title == "Report"
    assert any(
        b.text == "First paragraph of the report." and b.heading_path == ["Report"] for b in blocks
    )


def test_pdf_extraction_and_encrypted_pdf() -> None:
    _, blocks, pages, extractor = extract(Kind.PDF, pdf_bytes())
    assert (pages, extractor) == (3, "pdfplumber")
    assert {b.page for b in blocks} == {1, 2, 3}
    # Header, body and page number are separate blocks (vertical gaps), in reading order.
    assert [b.text for b in blocks if b.page == 2] == [
        "ACME Corp Confidential",
        "Body text for section 2 talks about topic 2.",
        "2",
    ]

    with pytest.raises(IngestPermanent, match="E_UNREADABLE"):
        extract(Kind.PDF, encrypted_pdf())


def test_pdf_headings_from_font_size_and_bold() -> None:
    data = make_pdf(
        [
            [
                (72, 60, "Field Guide", 24, False),
                (72, 120, "Getting started", 16, False),
                (72, 150, "Install the tool before you begin using it every day.", 12, False),
                (72, 164, "It takes a minute on most machines and needs no account.", 12, False),
                (72, 200, "Requirements", 12, True),
                (72, 220, "A recent operating system and a network connection.", 12, False),
            ],
            [
                (72, 60, "Troubleshooting", 16, False),
                (72, 90, "Restart the tool when it stops responding to commands.", 12, False),
            ],
        ]
    )
    title, blocks, _, _ = extract(Kind.PDF, data)

    assert title == "Field Guide"
    headings = [(b.text, b.level, b.heading_path) for b in blocks if b.type is BlockType.HEADING]
    assert headings == [
        ("Field Guide", 1, []),
        ("Getting started", 2, ["Field Guide"]),
        ("Requirements", 3, ["Field Guide", "Getting started"]),
        ("Troubleshooting", 2, ["Field Guide"]),
    ]
    body = next(b for b in blocks if b.text.startswith("A recent operating"))
    assert body.heading_path == ["Field Guide", "Getting started", "Requirements"]
    restart = next(b for b in blocks if b.text.startswith("Restart"))
    assert (restart.page, restart.heading_path) == (2, ["Field Guide", "Troubleshooting"])


def test_pdf_outline_marks_headings_and_bold_sentences_are_not_headings() -> None:
    base = make_pdf(
        [
            [
                (72, 60, "Overview"),
                (72, 90, "This plain line is body text that explains the overview."),
                (72, 130, "Bold text that ends like a sentence.", 12, True),
                (72, 160, "Details"),
                (72, 190, "More body text that belongs to the details section here."),
            ]
        ]
    )
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(base)))
    writer.add_outline_item("Overview", 0)
    writer.add_outline_item("Details", 0)
    buf = io.BytesIO()
    writer.write(buf)

    _, blocks, _, _ = extract(Kind.PDF, buf.getvalue())

    assert [b.text for b in blocks if b.type is BlockType.HEADING] == ["Overview", "Details"]
    assert blocks[-1].heading_path == ["Details"]


def test_running_headers_are_never_headings() -> None:
    pages = [
        [(72, 40, "ACME Corp Confidential", 12, True), (72, 100, f"Body text on page {n}.")]
        for n in range(1, 4)
    ]
    _, blocks, _, _ = extract(Kind.PDF, make_pdf(pages))
    assert not any(b.type is BlockType.HEADING for b in blocks)


def test_pdf_lines_close_together_form_one_paragraph() -> None:
    data = make_pdf([[(72, 100, "first line of a"), (72, 114, "paragraph that wraps")]])
    _, blocks, _, _ = extract(Kind.PDF, data)
    assert [b.text for b in blocks] == ["first line of a\nparagraph that wraps"]


# -- normalize ------------------------------------------------------------------------------


def test_clean_text() -> None:
    raw = "Caf\u00e9\u200b  na\u00efve\u00a0text  with  hyphen-\nated word\x07"
    assert clean_text(raw) == "Caf\u00e9 na\u00efve text with hyphenated word"


def test_running_headers_and_page_numbers_are_dropped() -> None:
    _, blocks, _, _ = extract(Kind.PDF, pdf_bytes(4))
    kept = [b.text for b in drop_running_headers(blocks)]
    assert not any("ACME Corp" in t for t in kept)
    assert not any(t.strip().isdigit() for t in kept)
    assert any("topic 2" in t for t in kept)


def test_normalize_keeps_case_and_table_lines() -> None:
    blocks = [
        Block(type=BlockType.PARAGRAPH, text="  Mixed CASE  stays "),
        Block(type=BlockType.TABLE, text="a | 1\nb | 2"),
        Block(type=BlockType.PARAGRAPH, text="\u200b  "),
    ]
    out = normalize_blocks(blocks)
    assert [b.text for b in out] == ["Mixed CASE stays", "a | 1\nb | 2"]


def test_iso_date() -> None:
    assert iso_date("2026-10-11T08:30:00+02:00") == "2026-10-11T06:30:00Z"
    assert iso_date("2026-10-11") == "2026-10-11T00:00:00Z"
    assert iso_date("yesterday") is None


# -- chunk --------------------------------------------------------------------------------


def doc(blocks: list[Block], title: str | None = "Title") -> NormalizedDocument:
    return NormalizedDocument(
        article_id="art_1", doc_version=2, pipeline_version="v1", title=title, blocks=blocks
    )


def test_chunks_respect_sections_size_and_overlap() -> None:
    tok = Tokenizer()
    sentence = "This sentence carries a little meaning about the topic at hand. "
    long_para = sentence * 60  # ~800 tokens
    blocks = [
        Block(type=BlockType.HEADING, text="One", level=1),
        Block(type=BlockType.PARAGRAPH, text=long_para, heading_path=["One"]),
        Block(type=BlockType.HEADING, text="Two", level=1),
        Block(type=BlockType.PARAGRAPH, text="Short section two text.", heading_path=["Two"]),
    ]
    chunks = Chunker(tok, ChunkerConfig(target=512, overlap=64, min_tokens=8)).chunk(doc(blocks))

    one = [c for c in chunks if c.heading_path == ["One"]]
    two = [c for c in chunks if c.heading_path == ["Two"]]
    assert len(one) >= 2
    assert len(two) == 1  # never mixed with section One
    assert all(c.token_count <= 512 + 64 for c in one)
    # Overlap: the second chunk starts with the tail of the first.
    assert one[1].text.split("\n\n")[0] in one[0].text
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert len({c.chunk_id for c in chunks}) == len(chunks)
    assert one[0].embed_text.startswith("Title \u203a One\n\n")


def test_tables_are_atomic_and_tiny_chunks_merge() -> None:
    table = "\n".join(f"row {i} | value {i}" for i in range(40))
    blocks = [
        Block(type=BlockType.PARAGRAPH, text="Lead in.", heading_path=["S"]),
        Block(type=BlockType.TABLE, text=table, heading_path=["S"]),
        Block(type=BlockType.PARAGRAPH, text="Tail.", heading_path=["S"]),
    ]
    chunks = Chunker(Tokenizer(), ChunkerConfig(target=512, min_tokens=64)).chunk(doc(blocks))
    assert len(chunks) == 1
    assert table in chunks[0].text


def test_table_continuations_repeat_the_header_row() -> None:
    table = "Name | Value\n" + "\n".join(f"row {i} | value {i}" for i in range(60))
    blocks = [Block(type=BlockType.TABLE, text=table, heading_path=["S"])]
    cfg = ChunkerConfig(target=100, max_tokens=120, min_tokens=8)
    chunks = Chunker(Tokenizer(), cfg).chunk(doc(blocks))

    assert len(chunks) > 2
    assert all(c.text.startswith("Name | Value\n") for c in chunks)
    rows = [line for c in chunks for line in c.text.split("\n")[1:]]
    assert rows == table.split("\n")[1:]  # every row once, in order
    assert all(c.token_count <= 120 for c in chunks)


def test_small_sibling_sections_merge_under_their_parent() -> None:
    answer = "Yes, it works offline after the first sync of your library."
    blocks = [
        Block(type=BlockType.HEADING, text="FAQ", level=1),
        Block(type=BlockType.HEADING, text="Offline?", heading_path=["FAQ"], level=2),
        Block(type=BlockType.PARAGRAPH, text=answer, heading_path=["FAQ", "Offline?"]),
        Block(type=BlockType.HEADING, text="Price?", heading_path=["FAQ"], level=2),
        Block(type=BlockType.PARAGRAPH, text="It is free.", heading_path=["FAQ", "Price?"]),
    ]
    chunks = Chunker(Tokenizer(), ChunkerConfig(min_tokens=64)).chunk(doc(blocks))

    assert len(chunks) == 1
    assert chunks[0].heading_path == ["FAQ"]
    assert chunks[0].text == f"Offline?\n\n{answer}\n\nPrice?\n\nIt is free."


def test_a_heading_without_a_body_is_kept() -> None:
    long_para = "This sentence carries a little meaning about the topic at hand. " * 20
    blocks = [
        Block(type=BlockType.HEADING, text="Intro", level=1),
        Block(type=BlockType.PARAGRAPH, text=long_para, heading_path=["Intro"]),
        Block(type=BlockType.HEADING, text="Appendix B: Glossary", level=1),
    ]
    chunks = Chunker(Tokenizer(), ChunkerConfig(min_tokens=8)).chunk(doc(blocks))

    last = chunks[-1]
    assert (last.text, last.heading_path) == ("Appendix B: Glossary", ["Appendix B: Glossary"])


def test_chunks_cite_their_span_of_the_normalized_text() -> None:
    sentence = "This sentence carries a little meaning about the topic at hand. "
    blocks = [
        Block(type=BlockType.HEADING, text="One", level=1),
        Block(type=BlockType.PARAGRAPH, text=(sentence * 60).strip(), heading_path=["One"]),
        Block(type=BlockType.PARAGRAPH, text="Closing words of section one.", heading_path=["One"]),
    ]
    d = doc(blocks)
    chunks = Chunker(Tokenizer(), ChunkerConfig(target=200, overlap=0, min_tokens=8)).chunk(d)

    full = d.text()
    assert len(chunks) > 1
    for c in chunks:
        assert c.char_start is not None
        assert c.char_end is not None
        span = full[c.char_start : c.char_end]
        assert span.startswith(c.text.split("\n\n")[0])
        assert span.endswith(c.text.split("\n\n")[-1])
    assert chunks[-1].char_end == len(full)


def test_parent_child_chunks_point_at_their_section_parent() -> None:
    sentence = "This sentence carries a little meaning about the topic at hand. "
    blocks = [
        Block(type=BlockType.PARAGRAPH, text=sentence * 40, heading_path=["A"]),
        Block(type=BlockType.PARAGRAPH, text=sentence * 10, heading_path=["B"]),
    ]
    cfg = ChunkerConfig(parent_child=True, parent_target=400, child_target=120, child_min=40)
    chunks = Chunker(Tokenizer(), cfg).chunk(doc(blocks))

    assert all(c.parent_id for c in chunks)
    assert all(c.token_count <= 120 + 32 for c in chunks)
    by_parent: dict[str, list[str]] = {}
    for c in chunks:
        by_parent.setdefault(str(c.parent_id), []).append(c.text)
        assert c.parent_text is None or c.text.split("\n\n")[-1] in c.parent_text
    assert len(by_parent) >= 3  # A splits into parents; B is its own
    assert {tuple(c.heading_path) for c in chunks} == {("A",), ("B",)}
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    # Off by default: no parents.
    assert all(c.parent_id is None for c in Chunker(Tokenizer()).chunk(doc(blocks)))


def test_embed_text_without_title_or_path() -> None:
    assert embed_text(None, [], "body") == "body"
    assert embed_text(None, ["A", "B"], "body") == "A > B\n\nbody"


def test_corrupt_pdf_is_unreadable() -> None:
    with pytest.raises(IngestPermanent, match="E_UNREADABLE"):
        extract(Kind.PDF, b"%PDF-1.4\nthis is not really a pdf")
