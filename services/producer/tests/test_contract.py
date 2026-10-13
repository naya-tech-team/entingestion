"""API contract hardening: idempotency keys, preconditions, listing, file types."""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.conftest import BUCKET, OTHER_ARN, REGION, ApiCall
from tests.test_api import create_process, put_article, ticket_body
from tests.test_registrar import deliver, key_of, new_process, s3_event, ticket, upload

KEY = {"Idempotency-Key": "retry-1"}


# -- idempotency ------------------------------------------------------------------------------


def test_a_retried_create_returns_the_first_process(api: ApiCall) -> None:
    body = {"name": "Batch", "source": "news"}
    first = api("POST", "/v1/processes", body, headers=KEY)
    again = api("POST", "/v1/processes", body, headers=KEY)

    assert (first.status, again.status) == (201, 201)
    assert again.body["process_id"] == first.body["process_id"]
    other = api("POST", "/v1/processes", body, headers={"Idempotency-Key": "retry-2"})
    assert other.body["process_id"] != first.body["process_id"]


def test_a_key_reused_with_another_body_is_refused(api: ApiCall) -> None:
    api("POST", "/v1/processes", {"name": "a", "source": "news"}, headers=KEY)

    resp = api("POST", "/v1/processes", {"name": "b", "source": "news"}, headers=KEY)

    assert resp.status == 422


def test_keys_are_per_caller(api: ApiCall) -> None:
    mine = api("POST", "/v1/processes", {"name": "a", "source": "news"}, headers=KEY)
    theirs = api(
        "POST", "/v1/processes", {"name": "a", "source": "sports"}, headers=KEY, caller=OTHER_ARN
    )

    assert theirs.status == 201
    assert theirs.body["process_id"] != mine.body["process_id"]


def test_a_retried_ticket_is_the_same_ticket(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]
    first = api("POST", f"/v1/processes/{pid}/uploads", ticket_body(), headers=KEY)
    again = api("POST", f"/v1/processes/{pid}/uploads", ticket_body(), headers=KEY)

    assert again.body["upload_id"] == first.body["upload_id"]
    assert api("GET", f"/v1/processes/{pid}").body["tickets_issued"] == 1


def test_failed_requests_are_not_remembered(api: ApiCall) -> None:
    bad = {"name": "a", "source": "unconfigured"}
    assert api("POST", "/v1/processes", bad, headers=KEY).status == 403
    good = api("POST", "/v1/processes", {"name": "a", "source": "news"}, headers=KEY)
    assert good.status == 201


@pytest.mark.parametrize("key", ["", "x" * 129, "has space"])
def test_bad_keys_are_rejected(api: ApiCall, key: str) -> None:
    resp = api(
        "POST", "/v1/processes", {"name": "a", "source": "news"}, headers={"Idempotency-Key": key}
    )
    assert resp.status == 422


# -- preconditions and listing ------------------------------------------------------------------


def test_patch_with_a_stale_etag_is_refused(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]
    put_article("art_e", pid, "2026-10-11T00:00:00.000Z")
    etag = api("GET", "/v1/articles/art_e").headers["ETag"]

    first = api("PATCH", "/v1/articles/art_e", {"title": "a"}, headers={"If-Match": etag})
    stale = api("PATCH", "/v1/articles/art_e", {"title": "b"}, headers={"If-Match": etag})

    assert first.status == 200
    assert first.headers["ETag"] != etag
    assert stale.status == 412
    assert api("GET", "/v1/articles/art_e").body["title"] == "a"


def test_processes_are_listed_per_source(api: ApiCall) -> None:
    ids = [create_process(api)["process_id"] for _ in range(3)]

    page1 = api("GET", "/v1/processes", query={"source": "news", "limit": "2"}).body
    page2 = api(
        "GET", "/v1/processes", query={"source": "news", "next_token": page1["next_token"]}
    ).body

    listed = [p["process_id"] for p in page1["items"] + page2["items"]]
    assert sorted(listed) == sorted(ids)
    assert api("GET", "/v1/processes", query={"source": "news"}, caller=OTHER_ARN).status == 403


# -- uploads ---------------------------------------------------------------------------------------


def test_the_presigned_put_pins_the_size(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]
    t = api("POST", f"/v1/processes/{pid}/uploads", ticket_body()).body

    signed = parse_qs(urlsplit(t["url"]).query)["X-Amz-SignedHeaders"][0].split(";")

    assert "content-length" in signed


@pytest.mark.parametrize(
    ("declared", "data", "ok"),
    [
        ("application/pdf", b"%PDF-1.7 a real pdf", True),
        ("application/pdf", b"plain text pretending", False),
        ("text/plain", "Unicode text é ✓".encode(), True),
        ("text/plain", b"%PDF-1.7\x00\x01binary", False),
        ("text/markdown", b"# Title\n\nBody", True),
    ],
)
def test_the_bytes_must_match_the_declared_type(
    api: ApiCall, declared: str, data: bytes, ok: bool
) -> None:
    pid = new_process(api)
    body: dict[str, Any] = {"content_type": declared, "filename": "f.bin"}
    t = ticket(api, pid, data, **body)
    _s3().put_object(Bucket=BUCKET, Key=key_of(t, "f.bin"), Body=data,
                     Metadata={"upload-id": t["upload_id"]},
                     ChecksumSHA256=_b64sha(data))  # fmt: skip

    deliver(s3_event(key_of(t, "f.bin")))

    got = upload(t["upload_id"])
    assert got["status"] == ("REGISTERED" if ok else "REJECTED")
    if not ok:
        assert got["reason"] == "content_type_mismatch"


def _s3() -> Any:
    import boto3

    return boto3.client("s3", region_name=REGION)


def _b64sha(data: bytes) -> str:
    import base64

    return base64.b64encode(hashlib.sha256(data).digest()).decode()
