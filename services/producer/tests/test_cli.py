from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import boto3
import pytest

from producer_client import cli
from producer_client.client import ProducerClient, guess_content_type
from producer_client.sigv4 import Signer, region_from_url
from tests.conftest import BUCKET, REGION, FakeContext, rest_event

API_URL = "https://abc123.execute-api.us-west-2.amazonaws.com/prod"


@pytest.fixture
def routed_client(aws: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Send the CLI's API calls straight to the Lambda handler; S3 PUTs still go over HTTP."""
    from producer.api import lambda_handler

    def send(
        self: ProducerClient,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        q = {k: str(v) for k, v in (query or {}).items() if v is not None} or None
        resp = lambda_handler(rest_event(method, path, body, q), FakeContext())  # type: ignore[arg-type]
        text = resp.get("body") or ""
        return resp["statusCode"], json.loads(text) if text else None

    monkeypatch.setattr(ProducerClient, "_send", send)


def test_region_from_execute_api_url() -> None:
    assert region_from_url(API_URL) == "us-west-2"
    assert region_from_url("https://api.example.com") is None


def test_signer_uses_execute_api_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    signer = Signer(boto3.Session(region_name="us-west-2"), "us-west-2")

    headers = signer.sign("GET", f"{API_URL}/v1/processes/prc_1", None, {})

    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-west-2/execute-api/aws4_request" in headers["Authorization"]
    assert "X-Amz-Date" in headers


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.pdf", "application/pdf"),
        ("a.md", "text/markdown"),
        ("a.HTML", "text/html"),
        ("a.txt", "text/plain"),
        ("a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("a.unknownext", None),
    ],
)
def test_guess_content_type(name: str, expected: str | None) -> None:
    assert guess_content_type(Path(name)) == expected


def test_external_id_modes(tmp_path: Path) -> None:
    f = tmp_path / "sub" / "a.txt"

    assert cli.external_id_for(f, tmp_path, "path") == "sub/a.txt"
    assert cli.external_id_for(f, tmp_path, "filename") == "a.txt"
    assert cli.external_id_for(f, tmp_path, "none") is None


def test_iter_files_skips_hidden(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / ".hidden").write_text("h")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("b")

    assert cli.iter_files(tmp_path) == [tmp_path / "a.txt", tmp_path / "sub" / "b.md"]


def test_ingest_dir_uploads_every_file_and_closes(
    routed_client: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {"a.txt": b"alpha", "sub/b.md": b"# beta", "c.html": b"<p>gamma</p>"}
    for rel, data in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(data)

    code = cli.main(
        [
            "--api-url",
            API_URL,
            "ingest-dir",
            str(tmp_path),
            "--name",
            "t",
            "--source",
            "smoke",
            "--close",
        ]
    )

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["failed"] == []
    assert len(out["uploaded"]) == 3
    assert out["process"]["status"] == "CLOSED"
    assert out["process"]["expected_count"] == 3

    s3 = boto3.client("s3", region_name=REGION)
    for item in out["uploaded"]:
        rel = Path(item["file"]).relative_to(tmp_path).as_posix()
        key = f"raw/{item['article_id']}/{item['upload_id']}/{Path(rel).name}"
        body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        assert hashlib.sha256(body).hexdigest() == hashlib.sha256(files[rel]).hexdigest()


def test_upload_reports_unsupported_files(
    routed_client: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "ok.txt"
    good.write_text("ok")
    bad = tmp_path / "image.png"
    bad.write_bytes(b"\x89PNG")
    cli.main(["--api-url", API_URL, "create-process", "--name", "t", "--source", "smoke"])
    pid = json.loads(capsys.readouterr().out)["process_id"]

    code = cli.main(["--api-url", API_URL, "upload", pid, str(good), str(bad)])

    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert [Path(u["file"]).name for u in out["uploaded"]] == ["ok.txt"]
    assert [Path(f["file"]).name for f in out["failed"]] == ["image.png"]
    assert "422" in out["failed"][0]["error"]


def test_status_and_close(routed_client: None, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["--api-url", API_URL, "create-process", "--name", "t", "--source", "smoke"])
    pid = json.loads(capsys.readouterr().out)["process_id"]

    assert cli.main(["--api-url", API_URL, "close", pid, "--expected-count", "4"]) == 0
    assert json.loads(capsys.readouterr().out)["expected_count"] == 4

    assert cli.main(["--api-url", API_URL, "status", pid]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "CLOSED"

    assert cli.main(["--api-url", API_URL, "close", pid]) == 0  # a repeated close is fine
    assert cli.main(["--api-url", API_URL, "close", pid, "--expected-count", "9"]) == 2  # 409
