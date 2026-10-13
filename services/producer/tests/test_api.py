from __future__ import annotations

import base64
import hashlib
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
import requests

from tests.conftest import BUCKET, CALLER_ARN, REGION, ApiCall

PDF = b"%PDF-1.7 test document"
PDF_SHA = hashlib.sha256(PDF).hexdigest()


def create_process(api: ApiCall, **overrides: Any) -> dict[str, Any]:
    body = {"name": "Batch 1", "source": "news", **overrides}
    resp = api("POST", "/v1/processes", body)
    assert resp.status == 201, resp.body
    return dict(resp.body)


def ticket_body(**overrides: Any) -> dict[str, Any]:
    return {
        "filename": "doc.pdf",
        "content_type": "application/pdf",
        "size_bytes": len(PDF),
        "sha256": PDF_SHA,
        "external_id": "doc-1",
        **overrides,
    }


def put_article(article_id: str, process_id: str, updated_at: str, **extra: Any) -> None:
    """Articles are written by the registrar; tests seed them directly, with the
    process membership marker the registrar writes."""
    boto3.resource("dynamodb", region_name=REGION).Table("producer_uploads").put_item(
        Item={
            "upload_id": f"MEMBER#{process_id}#{article_id}",
            "kind": "membership",
            "member_process_id": process_id,
            "member_article_id": article_id,
        }
    )
    table = boto3.resource("dynamodb", region_name=REGION).Table("articles")
    table.put_item(
        Item={
            "article_id": article_id,
            "process_id": process_id,
            "first_process_id": process_id,
            "source": "news",
            "filename": "doc.pdf",
            "content_type": "application/pdf",
            "s3_uri": f"s3://{BUCKET}/raw/{article_id}/upl_x/doc.pdf",
            "s3_key": f"raw/{article_id}/upl_x/doc.pdf",
            "content_sha256": PDF_SHA,
            "size_bytes": len(PDF),
            "upload_id": "upl_x",
            "status": "STAGED",
            "metadata": {},
            "created_at": updated_at,
            "updated_at": updated_at,
            "updated_date": updated_at[:10],
            **extra,
        }
    )


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------


def test_create_and_get_process(api: ApiCall) -> None:
    created = create_process(api, metadata={"team": "data"}, expected_count=3)

    assert created["process_id"].startswith("prc_")
    assert created["status"] == "OPEN"
    assert created["registered_count"] == 0
    assert created["rejected_count"] == 0
    assert (created["tickets_issued"], created["pending_uploads"]) == (0, 0)
    assert created["expected_count"] == 3
    assert created["created_by"] == CALLER_ARN
    assert created["settled"] is False

    fetched = api("GET", f"/v1/processes/{created['process_id']}")
    assert fetched.status == 200
    assert fetched.body == created


@pytest.mark.parametrize(
    "body",
    [
        {"name": "", "source": "news"},
        {"name": "x" * 201, "source": "news"},
        {"name": "ok", "source": "News"},
        {"name": "ok", "source": "-news"},
        {"name": "ok", "source": "news", "expected_count": -1},
        {"name": "ok", "source": "news", "metadata": {f"k{i}": "v" for i in range(51)}},
        {"name": "ok", "source": "news", "unexpected": True},
        {"source": "news"},
    ],
)
def test_create_process_validation(api: ApiCall, body: dict[str, Any]) -> None:
    resp = api("POST", "/v1/processes", body)

    assert resp.status == 422
    assert resp.body["message"]
    assert resp.body["errors"]
    assert all({"loc", "msg"} <= e.keys() for e in resp.body["errors"])
    assert all(e["loc"][0] == "body" for e in resp.body["errors"])


def test_unknown_body_field_is_reported(api: ApiCall) -> None:
    resp = api("POST", "/v1/processes", {"name": "ok", "source": "news", "colour": "red"})

    assert resp.status == 422
    assert ["body", "colour"] in [e["loc"] for e in resp.body["errors"]]


def test_invalid_json_and_non_object_bodies(api: ApiCall) -> None:
    assert api("POST", "/v1/processes", raw_body="{not json").status == 400  # unparseable
    assert api("POST", "/v1/processes", raw_body="[1, 2]").status == 422
    assert api("POST", "/v1/processes").status == 422


def test_get_unknown_process_is_404(api: ApiCall) -> None:
    resp = api("GET", "/v1/processes/prc_missing")

    assert resp.status == 404
    assert resp.body == {"message": "process prc_missing not found"}


def test_unknown_route_is_404(api: ApiCall) -> None:
    assert api("GET", "/v1/nope").status == 404


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


def test_close_defaults_expected_count_and_blocks_new_tickets(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]

    closed = api("POST", f"/v1/processes/{pid}/close")

    assert closed.status == 200
    assert closed.body["status"] == "CLOSED"
    assert closed.body["expected_count"] == 0  # registered_count at close time
    assert closed.body["closed_at"]
    assert closed.body["settled"] is True

    assert api("POST", f"/v1/processes/{pid}/uploads", ticket_body()).status == 409
    again = api("POST", f"/v1/processes/{pid}/close")  # a retried close gets the same answer
    assert (again.status, again.body["closed_at"]) == (200, closed.body["closed_at"])
    assert api("POST", f"/v1/processes/{pid}/close", {"expected_count": 5}).status == 409


def test_close_with_explicit_expected_count(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]

    closed = api("POST", f"/v1/processes/{pid}/close", {"expected_count": 2})

    assert closed.status == 200
    assert closed.body["expected_count"] == 2
    assert closed.body["settled"] is False


def test_close_unknown_process_is_404(api: ApiCall) -> None:
    assert api("POST", "/v1/processes/prc_missing/close").status == 404


def test_close_rejects_unknown_fields(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]

    assert api("POST", f"/v1/processes/{pid}/close", {"force": True}).status == 422


# ---------------------------------------------------------------------------
# Upload tickets
# ---------------------------------------------------------------------------


def test_ticket_is_presigned_with_checksum_and_no_article_row_yet(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]

    resp = api("POST", f"/v1/processes/{pid}/uploads", ticket_body(title="Doc"))

    assert resp.status == 201
    ticket = resp.body
    assert ticket["method"] == "PUT"
    assert ticket["upload_id"].startswith("upl_")
    assert ticket["article_id"].startswith("art_")
    assert ticket["process_id"] == pid
    assert ticket["expires_at"]

    parts = urlsplit(ticket["url"])
    # Regional host: the global one redirects new buckets, which breaks the signature.
    assert parts.netloc == f"{BUCKET}.s3.{REGION}.amazonaws.com"
    assert parts.path == f"/raw/{ticket['article_id']}/{ticket['upload_id']}/doc.pdf"
    query = {k.lower() for k in parse_qs(parts.query)}
    assert "x-amz-signature" in query

    expected_checksum = base64.b64encode(bytes.fromhex(PDF_SHA)).decode()
    sent = {k.lower(): v for k, v in ticket["headers"].items()}
    # Each required header is either returned for the client to send or already in the URL.
    for name, value in {
        "content-type": "application/pdf",
        "x-amz-checksum-sha256": expected_checksum,
        "x-amz-meta-upload-id": ticket["upload_id"],
    }.items():
        assert sent.get(name) == value or name in query

    upload = api("GET", f"/v1/uploads/{ticket['upload_id']}")
    assert upload.status == 200
    assert upload.body["status"] == "PENDING"
    assert upload.body["requested_by"] == CALLER_ARN
    assert upload.body["sha256"] == PDF_SHA
    assert upload.body["s3_key"] == f"raw/{ticket['article_id']}/{ticket['upload_id']}/doc.pdf"

    assert api("GET", f"/v1/articles/{ticket['article_id']}").status == 404


def test_pending_uploads_outlive_the_registrar_dlq(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]
    ticket = api("POST", f"/v1/processes/{pid}/uploads", ticket_body()).body

    item = (
        boto3.resource("dynamodb", region_name=REGION)
        .Table("producer_uploads")
        .get_item(Key={"upload_id": ticket["upload_id"]})["Item"]
    )
    # The row lasts 15 days (a redrive from the 14-day DLQ still finds it); the ticket itself
    # expires with the presigned URL, and the janitor settles it if nothing arrived.
    assert abs(int(item["expires_at_epoch"]) - 15 * 86400 - time.time()) < 60
    assert abs(int(item["ticket_expires_epoch"]) - 900 - time.time()) < 60
    assert (item["pending"], int(item["upload_seq"])) == ("P", 1)


def test_client_put_with_ticket_headers_succeeds(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]
    ticket = api("POST", f"/v1/processes/{pid}/uploads", ticket_body()).body

    put = requests.put(ticket["url"], data=PDF, headers=ticket["headers"], timeout=10)

    assert put.status_code == 200, put.text
    key = f"raw/{ticket['article_id']}/{ticket['upload_id']}/doc.pdf"
    head = boto3.client("s3", region_name=REGION).head_object(Bucket=BUCKET, Key=key)
    assert head["ContentLength"] == len(PDF)
    assert head["Metadata"] == {"upload-id": ticket["upload_id"]}


def test_same_external_id_maps_to_same_article(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]
    other = create_process(api, source="blog")["process_id"]

    a = api("POST", f"/v1/processes/{pid}/uploads", ticket_body()).body
    b = api("POST", f"/v1/processes/{pid}/uploads", ticket_body()).body
    c = api("POST", f"/v1/processes/{other}/uploads", ticket_body()).body

    assert a["article_id"] == b["article_id"]
    assert a["upload_id"] != b["upload_id"]
    assert c["article_id"] != a["article_id"]  # same external_id, different source


def test_no_external_id_gives_new_article_each_time(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]
    body = ticket_body()
    del body["external_id"]

    a = api("POST", f"/v1/processes/{pid}/uploads", body).body
    b = api("POST", f"/v1/processes/{pid}/uploads", body).body

    assert a["article_id"] != b["article_id"]


def test_disallowed_content_type_is_422(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]

    resp = api("POST", f"/v1/processes/{pid}/uploads", ticket_body(content_type="image/png"))

    assert resp.status == 422
    assert resp.body["errors"][0]["loc"] == ["body", "content_type"]


def test_file_over_max_size_is_422(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]

    resp = api("POST", f"/v1/processes/{pid}/uploads", ticket_body(size_bytes=10 * 1024 * 1024 + 1))

    assert resp.status == 422
    assert resp.body["errors"][0]["loc"] == ["body", "size_bytes"]


@pytest.mark.parametrize(
    "filename",
    ["../etc/passwd", "a/b.pdf", "a\\b.pdf", "..", ".", "bad\x00.pdf", "tab\t.pdf", "x" * 256, ""],
)
def test_unsafe_filenames_are_rejected(api: ApiCall, filename: str) -> None:
    pid = create_process(api)["process_id"]

    resp = api("POST", f"/v1/processes/{pid}/uploads", ticket_body(filename=filename))

    assert resp.status == 422
    assert resp.body["errors"][0]["loc"] == ["body", "filename"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"sha256": "abc"},
        {"sha256": "g" * 64},
        {"size_bytes": 0},
        {"external_id": "x" * 257},
        {"unexpected": 1},
    ],
)
def test_ticket_field_validation(api: ApiCall, overrides: dict[str, Any]) -> None:
    pid = create_process(api)["process_id"]

    assert api("POST", f"/v1/processes/{pid}/uploads", ticket_body(**overrides)).status == 422


def test_uppercase_sha256_is_normalised(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]

    ticket = api("POST", f"/v1/processes/{pid}/uploads", ticket_body(sha256=PDF_SHA.upper())).body

    assert api("GET", f"/v1/uploads/{ticket['upload_id']}").body["sha256"] == PDF_SHA


def test_ticket_for_unknown_process_is_404(api: ApiCall) -> None:
    assert api("POST", "/v1/processes/prc_missing/uploads", ticket_body()).status == 404


def test_unknown_upload_is_404(api: ApiCall) -> None:
    assert api("GET", "/v1/uploads/upl_missing").status == 404


# ---------------------------------------------------------------------------
# Articles
# ---------------------------------------------------------------------------


def test_get_patch_and_delete_article(api: ApiCall) -> None:
    put_article("art_1", "prc_1", "2026-10-10T10:00:00.000Z", title="Old", metadata={"a": "1"})

    got = api("GET", "/v1/articles/art_1")
    assert got.status == 200
    assert got.body["title"] == "Old"

    patched = api("PATCH", "/v1/articles/art_1", {"title": "New"})
    assert patched.status == 200
    assert patched.body["title"] == "New"
    assert patched.body["metadata"] == {"a": "1"}  # untouched
    assert patched.body["updated_at"] > "2026-10-10T10:00:00.000Z"
    assert patched.body["updated_date"] == patched.body["updated_at"][:10]

    patched = api("PATCH", "/v1/articles/art_1", {"metadata": {"b": "2"}, "title": None})
    assert patched.body["metadata"] == {"b": "2"}
    assert patched.body.get("title") is None

    assert api("DELETE", "/v1/articles/art_1").status == 204
    assert api("GET", "/v1/articles/art_1").status == 404
    assert api("DELETE", "/v1/articles/art_1").status == 404


def test_patch_requires_a_field_and_rejects_others(api: ApiCall) -> None:
    put_article("art_1", "prc_1", "2026-10-10T10:00:00.000Z")

    assert api("PATCH", "/v1/articles/art_1", {}).status == 422
    assert api("PATCH", "/v1/articles/art_1", {"status": "GONE"}).status == 422
    assert api("PATCH", "/v1/articles/art_missing", {"title": "x"}).status == 404


def test_list_articles_paginates(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]
    for i in range(3):
        put_article(f"art_{i}", pid, f"2026-10-10T10:00:0{i}.000Z")
    put_article("art_other", "prc_other", "2026-10-10T10:00:00.000Z")

    seen: list[str] = []
    token: str | None = None
    for _ in range(5):
        query = {"limit": "2"} | ({"next_token": token} if token else {})
        page = api("GET", f"/v1/processes/{pid}/articles", query=query)
        assert page.status == 200
        seen += [a["article_id"] for a in page.body["items"]]
        token = page.body["next_token"]
        if token is None:
            break

    assert sorted(seen) == ["art_0", "art_1", "art_2"]


@pytest.mark.parametrize(
    "query",
    [{"limit": "0"}, {"limit": "501"}, {"limit": "x"}, {"next_token": "!!!"}, {"other": "1"}],
)
def test_list_articles_query_validation(api: ApiCall, query: dict[str, str]) -> None:
    pid = create_process(api)["process_id"]

    resp = api("GET", f"/v1/processes/{pid}/articles", query=query)

    assert resp.status == 422
    assert resp.body["errors"][0]["loc"][0] == "query"


def test_list_articles_unknown_process_is_404(api: ApiCall) -> None:
    assert api("GET", "/v1/processes/prc_missing/articles").status == 404


def test_the_api_needs_its_idempotency_table(api: ApiCall, monkeypatch: pytest.MonkeyPatch) -> None:
    from producer.api import get_service
    from producer.config import MissingSetting, get_settings

    monkeypatch.delenv("IDEMPOTENCY_TABLE")
    get_settings.cache_clear()
    get_service.cache_clear()

    with pytest.raises(MissingSetting, match="IDEMPOTENCY_TABLE"):
        api("GET", "/v1/sources")
