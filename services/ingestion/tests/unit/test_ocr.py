"""OCR path for scanned and very large PDFs (brief §4.4, UC12), with Textract faked."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import boto3
import pytest
from pypdf import PdfWriter

from ingest import aws
from ingest.errors import ErrorCode, IngestPermanent
from ingest.extract.ocr import blocks_from_textract, title_of
from ingest.ids import new_run_id
from ingest.models.staging import BlockType
from ingest.models.status import StepName
from ingest.status import keys
from tests.conftest import REGION
from tests.unit.test_pipeline import (
    ORDER,
    FakeContext,
    FakeTitan,
    FakeVectorStore,
    handlers,
    latest,
    message_from,
    produce,
    summary,
)

Env = tuple[FakeVectorStore, FakeTitan]
SENTENCES = [f"Scanned page sentence number {i} has enough words to be indexed." for i in range(40)]


def scanned_pdf(pages: int = 2) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def line(text: str, page: int, top: float, height: float = 0.02) -> dict[str, Any]:
    return {
        "BlockType": "LINE",
        "Text": text,
        "Page": page,
        "Geometry": {"BoundingBox": {"Top": top, "Height": height, "Left": 0.1, "Width": 0.8}},
    }


class FakeTextract:
    """Writes its result to the OutputConfig location, like the real async API."""

    def __init__(self, pages: int = 2) -> None:
        self.pages = pages
        self.started: list[str] = []

    def start_document_analysis(self, **kwargs: Any) -> dict[str, str]:
        assert kwargs["FeatureTypes"] == ["LAYOUT", "TABLES"]
        job = f"job-{kwargs['ClientRequestToken']}"
        self.started.append(job)
        out = kwargs["OutputConfig"]
        s3 = boto3.client("s3", region_name=REGION)
        for page in range(1, self.pages + 1):
            blocks = [line(t, page, 0.05 + 0.03 * i) for i, t in enumerate(SENTENCES[:20])]
            blocks += [line(t, page, 0.8 + 0.025 * i) for i, t in enumerate(SENTENCES[20:24])]
            part = {"DocumentMetadata": {"Pages": self.pages}, "Blocks": blocks}
            s3.put_object(
                Bucket=out["S3Bucket"], Key=f"{out['S3Prefix']}/{job}/{page}",
                Body=json.dumps(part).encode(),
            )  # fmt: skip
        s3.put_object(Bucket=out["S3Bucket"], Key=f"{out['S3Prefix']}/{job}/.s3_access_check",
                      Body=b"")  # fmt: skip
        return {"JobId": job}

    def get_document_analysis(self, JobId: str, MaxResults: int) -> dict[str, Any]:
        return {"JobStatus": "SUCCEEDED"}


@pytest.fixture
def textract(env: Env, monkeypatch: pytest.MonkeyPatch) -> FakeTextract:
    fake = FakeTextract()
    monkeypatch.setattr(aws, "textract", lambda: fake)
    return fake


def produce_pdf(article_id: str, data: bytes) -> dict[str, Any]:
    """produce() for binary PDF bodies (it writes .html keys; Fetch sniffs the bytes)."""
    import hashlib

    record = produce(article_id, "placeholder")
    image = record["dynamodb"]["NewImage"]
    bucket, key = image["s3_uri"]["S"].removeprefix("s3://").split("/", 1)
    key = key.rsplit("/", 1)[0] + "/scan.pdf"
    put = boto3.client("s3", region_name=REGION).put_object(Bucket=bucket, Key=key, Body=data)
    image["s3_uri"] = {"S": f"s3://{bucket}/{key}"}
    image["s3_version_id"] = {"S": put["VersionId"]}
    image["content_sha256"] = {"S": hashlib.sha256(data).hexdigest()}
    boto3.resource("dynamodb", region_name=REGION).Table("articles").update_item(
        Key={"article_id": article_id},
        UpdateExpression="SET s3_uri = :u, s3_version_id = :v, content_sha256 = :s",
        ExpressionAttributeValues={
            ":u": image["s3_uri"]["S"], ":v": put["VersionId"], ":s": image["content_sha256"]["S"]
        },
    )  # fmt: skip
    return record


def run_ocr(message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Drive one execution like the state machine, including the OCR branch."""
    from ingest.steps import claim, ocr

    run_id = new_run_id()
    ctx = claim.handler({"ctx": {"message": message, "run_id": run_id}}, FakeContext())
    event = {"ctx": ctx}
    route = handlers()[StepName.FETCH](event, FakeContext())["route"]
    if route != "OCR":
        route = handlers()[StepName.EXTRACT](event, FakeContext())["route"]
    assert route == "OCR"
    job = ocr.start(event, FakeContext())["job_id"]
    assert ocr.poll(event | {"job_id": job}, FakeContext())["status"] == "SUCCEEDED"
    ocr.collect(event | {"job_id": job}, FakeContext())
    for name in ORDER[ORDER.index(StepName.NORMALIZE) :]:
        handlers()[name](event, FakeContext())
    return run_id, ctx


# -- Textract output -> blocks ---------------------------------------------------------------


def test_lines_become_paragraphs_per_page() -> None:
    part = {
        "DocumentMetadata": {"Pages": 2},
        "Blocks": [
            {"BlockType": "PAGE", "Page": 1},
            line("second line", 1, 0.13),
            line("first line", 1, 0.10),
            line("after a gap", 1, 0.40),
            line("page two", 2, 0.10),
            {"BlockType": "WORD", "Text": "ignored", "Page": 1},
        ],
    }
    blocks, pages = blocks_from_textract([part])
    assert pages == 2
    assert [(b.page, b.text) for b in blocks] == [
        (1, "first line\nsecond line"),
        (1, "after a gap"),
        (2, "page two"),
    ]


FIXTURE = Path(__file__).parent / "fixtures" / "textract_layout.json"


def test_layout_blocks_give_reading_order_headings_lists_and_tables() -> None:
    """A recorded-shape GetDocumentAnalysis result (LAYOUT + TABLES) in two output files."""
    blocks, pages = blocks_from_textract(json.loads(FIXTURE.read_text()))

    assert pages == 2
    assert title_of(blocks) == "Annual Report 2025"
    assert [(b.type, b.page, b.text.split("\n")[0]) for b in blocks] == [
        (BlockType.HEADING, 1, "Annual Report 2025"),
        (BlockType.HEADING, 1, "Revenue"),
        # Two columns, left then right: LAYOUT's reading order, not line positions.
        (BlockType.PARAGRAPH, 1, "Revenue grew in every region during"),
        (BlockType.PARAGRAPH, 1, "Costs stayed flat because the new"),
        (BlockType.LIST, 1, "Online orders doubled"),
        (BlockType.LIST, 1, "Store count rose to 40"),
        (BlockType.TABLE, 1, "Region | Revenue"),
        (BlockType.HEADING, 2, "Outlook"),
        # Its LINE is in the first output file: relationships cross files.
        (BlockType.PARAGRAPH, 2, "We expect steady growth next year."),
    ]
    table = next(b for b in blocks if b.type is BlockType.TABLE)
    assert table.text == "Region | Revenue\nWest | 120\nEast | 95"
    assert blocks[-1].heading_path == ["Annual Report 2025", "Outlook"]
    texts = " ".join(b.text for b in blocks)
    # Running header, page number and chart labels are dropped.
    assert "ACME Corp" not in texts
    assert "Chart label" not in texts
    assert not any(b.text == "1" for b in blocks)


def test_lines_without_layout_read_two_columns_in_order() -> None:
    def at(text: str, left: float, top: float) -> dict[str, Any]:
        block = line(text, 1, top)
        block["Geometry"]["BoundingBox"] |= {"Left": left, "Width": 0.38}
        return block

    left = [at(f"left {i}", 0.08, 0.2 + 0.02 * i) for i in range(3)]
    right = [at(f"right {i}", 0.54, 0.2 + 0.02 * i) for i in range(3)]
    title = line("Spanning title", 1, 0.05)
    blocks, _ = blocks_from_textract([{"Blocks": [title, *right, *left]}])

    assert [b.text for b in blocks] == [
        "Spanning title",
        "left 0\nleft 1\nleft 2",
        "right 0\nright 1\nright 2",
    ]


# -- routing ---------------------------------------------------------------------------------


def test_scanned_pdf_is_handed_to_ocr_and_indexed(env: Env, textract: FakeTextract) -> None:
    vectors, _ = env
    message = message_from(produce_pdf("art_1", scanned_pdf()))

    run_id, _ = run_ocr(message)

    assert latest("art_1")["status"] == "INDEXED"
    assert vectors.active("art_1")
    items = aws.status_store().run_items("art_1", run_id)
    assert len(items) + 1 == 10  # RUN# + 8 steps + LATEST, as for any text document
    extract = aws.status_store().get(keys.step_key("art_1", run_id, StepName.EXTRACT)) or {}
    assert (extract["step_status"], extract["attempt"]) == ("SUCCEEDED", 1)  # not a retry
    assert extract["metrics"]["extractor"] == "textract"
    assert summary("prc_A")["indexed"] == 1
    assert textract.started == [f"job-{run_id}"]


def test_large_pdf_goes_straight_to_ocr(
    env: Env, textract: FakeTextract, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAMBDA_MAX_SOURCE_BYTES", "100")
    aws.reset()
    message = message_from(produce_pdf("art_1", scanned_pdf(3)))

    run_id, _ = run_ocr(message)

    assert latest("art_1")["status"] == "INDEXED"
    fetch = aws.status_store().get(keys.step_key("art_1", run_id, StepName.FETCH)) or {}
    assert fetch["metrics"]["route"] == "OCR"


def test_large_non_pdf_is_too_large(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from ingest.steps import claim

    monkeypatch.setenv("LAMBDA_MAX_SOURCE_BYTES", "100")
    aws.reset()
    message = message_from(produce("art_1", "<html>" + "x" * 500 + "</html>"))
    ctx = claim.handler({"ctx": {"message": message, "run_id": new_run_id()}}, FakeContext())

    with pytest.raises(IngestPermanent) as err:
        handlers()[StepName.FETCH]({"ctx": ctx}, FakeContext())
    assert err.value.code == ErrorCode.TOO_LARGE


def test_text_pdf_stays_in_lambda(env: Env) -> None:
    from ingest.steps import claim
    from tests.unit.test_extract_normalize_chunk import pdf_bytes

    message = message_from(produce_pdf("art_1", pdf_bytes(2)))
    ctx = claim.handler({"ctx": {"message": message, "run_id": new_run_id()}}, FakeContext())
    assert handlers()[StepName.FETCH]({"ctx": ctx}, FakeContext())["route"] == "LAMBDA"
    assert handlers()[StepName.EXTRACT]({"ctx": ctx}, FakeContext())["route"] == "LAMBDA"


def test_failed_textract_job_is_permanent(env: Env, textract: FakeTextract,
                                          monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    from ingest.steps import claim, ocr

    message = message_from(produce_pdf("art_1", scanned_pdf()))
    ctx = claim.handler({"ctx": {"message": message, "run_id": new_run_id()}}, FakeContext())
    event = {"ctx": ctx}
    handlers()[StepName.FETCH](event, FakeContext())
    handlers()[StepName.EXTRACT](event, FakeContext())
    job = ocr.start(event, FakeContext())["job_id"]
    monkeypatch.setattr(
        textract, "get_document_analysis",
        lambda **_: {"JobStatus": "FAILED", "StatusMessage": "unsupported document"},
    )  # fmt: skip

    with pytest.raises(IngestPermanent) as err:
        ocr.poll(event | {"job_id": job}, FakeContext())
    assert err.value.code == ErrorCode.UNREADABLE


def test_ocr_polling_gives_up_after_its_cap(env: Env, textract: FakeTextract,
                                            monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    from ingest.errors import IngestTransient
    from ingest.steps import claim, ocr

    monkeypatch.setenv("OCR_MAX_POLLS", "3")
    aws.reset()
    message = message_from(produce_pdf("art_1", scanned_pdf()))
    ctx = claim.handler({"ctx": {"message": message, "run_id": new_run_id()}}, FakeContext())
    event = {"ctx": ctx}
    handlers()[StepName.FETCH](event, FakeContext())
    handlers()[StepName.EXTRACT](event, FakeContext())
    job = ocr.start(event, FakeContext())["job_id"]
    monkeypatch.setattr(textract, "get_document_analysis", lambda **_: {"JobStatus": "IN_PROGRESS"})

    # The state machine carries the count from poll to poll.
    first = ocr.poll(event | {"job_id": job}, FakeContext())
    second = ocr.poll(event | {"job_id": job, "polls": first["polls"]}, FakeContext())
    assert (first["polls"], second["polls"]) == (1, 2)
    with pytest.raises(IngestTransient) as err:  # MarkFailed: FAILED_RETRYABLE, retried later
        ocr.poll(event | {"job_id": job, "polls": second["polls"]}, FakeContext())
    assert err.value.code == ErrorCode.TIMEOUT
    assert "after 3 status checks" in err.value.detail


def test_with_ocr_off_scanned_pdfs_are_skipped(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.test_pipeline import run

    monkeypatch.setenv("OCR_ENABLED", "false")
    aws.reset()

    _, outcome = run(message_from(produce_pdf("art_1", scanned_pdf())))

    assert outcome["action"] == "FAILED"  # the harness reports MarkFailed's runs as FAILED
    assert latest("art_1")["status"] == "SKIPPED"
    assert latest("art_1")["skip_reason"] == "E_EMPTY_TEXT"
    assert summary("prc_A")["skipped"] == 1


def test_with_ocr_off_large_pdfs_are_too_large(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from ingest.steps import claim

    monkeypatch.setenv("OCR_ENABLED", "false")
    monkeypatch.setenv("LAMBDA_MAX_SOURCE_BYTES", "100")
    aws.reset()
    message = message_from(produce_pdf("art_1", scanned_pdf(3)))
    ctx = claim.handler({"ctx": {"message": message, "run_id": new_run_id()}}, FakeContext())

    with pytest.raises(IngestPermanent) as err:
        handlers()[StepName.FETCH]({"ctx": ctx}, FakeContext())
    assert err.value.code == ErrorCode.TOO_LARGE
