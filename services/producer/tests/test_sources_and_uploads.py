"""The reads behind the upload UI: GET /v1/sources and GET /v1/processes/{id}/uploads."""

from __future__ import annotations

import time
from typing import Any

import pytest

from tests.conftest import ADMIN_ARN, BUCKET, CALLER_ARN, OTHER_ARN, READER_ARN, ApiCall
from tests.test_registrar import (
    PDF,
    deliver,
    key_of,
    new_process,
    s3,
    s3_event,
    ticket,
    upload_and_register,
)

# -- sources ---------------------------------------------------------------------------------


def test_writers_see_the_sources_they_write(api: ApiCall) -> None:
    resp = api("GET", "/v1/sources")

    assert resp.status == 200
    assert resp.body == {
        "items": [
            {"source": "blog", "tenant_id": "t-blog", "access": "write"},
            {"source": "news", "tenant_id": "t-news", "access": "write"},
            {"source": "smoke", "tenant_id": "t-smoke", "access": "write"},
        ]
    }


def test_readers_see_their_sources_as_read(api: ApiCall) -> None:
    resp = api("GET", "/v1/sources", caller=READER_ARN)

    assert resp.body == {"items": [{"source": "news", "tenant_id": "t-news", "access": "read"}]}


def test_admins_see_every_source_as_write(api: ApiCall) -> None:
    items = api("GET", "/v1/sources", caller=ADMIN_ARN).body["items"]

    assert [i["source"] for i in items] == ["blog", "news", "smoke", "sports"]
    assert {i["access"] for i in items} == {"write"}


def test_callers_without_access_see_no_sources(api: ApiCall) -> None:
    resp = api("GET", "/v1/sources", caller="arn:aws:iam::123456789012:user/nobody")

    assert (resp.status, resp.body) == (200, {"items": []})


# -- a process's uploads ---------------------------------------------------------------------


def uploads(api: ApiCall, pid: str, caller: str = CALLER_ARN) -> list[dict[str, Any]]:
    resp = api("GET", f"/v1/processes/{pid}/uploads", caller=caller)
    assert resp.status == 200, resp.body
    return list(resp.body["items"])


def test_every_upload_is_listed_oldest_first_whatever_its_outcome(api: ApiCall) -> None:
    pid = new_process(api)
    registered = upload_and_register(api, pid, external_id="a", title="First")
    time.sleep(0.002)  # created_at has millisecond precision
    rejected = ticket(api, pid, external_id="b")
    s3().put_object(Bucket=BUCKET, Key=key_of(rejected), Body=b"%PDF-1.7 registrar tesX")
    deliver(s3_event(key_of(rejected)))
    time.sleep(0.002)
    pending = ticket(api, pid, filename="c.pdf", external_id=None)

    items = uploads(api, pid)

    # The registrar's membership marker for the registered article isn't an upload.
    assert [i["upload_id"] for i in items] == [
        registered["upload_id"],
        rejected["upload_id"],
        pending["upload_id"],
    ]
    assert [(i["status"], i["reason"]) for i in items] == [
        ("REGISTERED", None),
        ("REJECTED", "sha256_mismatch"),
        ("PENDING", None),
    ]
    first = items[0]
    assert first == {
        "upload_id": registered["upload_id"],
        "article_id": registered["article_id"],
        "filename": "doc.pdf",
        "content_type": "application/pdf",
        "size_bytes": len(PDF),
        "status": "REGISTERED",
        "reason": None,
        "title": "First",
        "external_id": "a",
        "created_at": first["created_at"],
        "finished_at": first["finished_at"],
    }
    assert first["finished_at"] is not None
    assert items[2]["finished_at"] is None
    assert items[2]["external_id"] is None


def test_uploads_page_through_without_gaps(api: ApiCall) -> None:
    pid = new_process(api)
    upload_and_register(api, pid, external_id="x0")  # leaves a membership marker in the index
    issued = [ticket(api, pid, external_id=f"x{i}")["upload_id"] for i in range(1, 5)]
    ticket(api, new_process(api), external_id="elsewhere")

    seen: list[str] = []
    token: str | None = None
    for _ in range(10):
        query = {"limit": "2"} | ({"next_token": token} if token else {})
        resp = api("GET", f"/v1/processes/{pid}/uploads", query=query)
        assert resp.status == 200
        assert len(resp.body["items"]) <= 2
        seen += [i["upload_id"] for i in resp.body["items"]]
        token = resp.body["next_token"]
        if token is None:
            break

    assert len(seen) == 5
    assert set(issued) <= set(seen)


def test_a_next_token_only_pages_its_own_process(api: ApiCall) -> None:
    pid, other = new_process(api), new_process(api)
    for i in range(2):
        ticket(api, other, external_id=f"o{i}")
    token = api("GET", f"/v1/processes/{other}/uploads", query={"limit": "1"}).body["next_token"]

    resp = api("GET", f"/v1/processes/{pid}/uploads", query={"next_token": token})

    assert resp.status == 422
    assert resp.body["errors"] == [
        {"loc": ["query", "next_token"], "msg": "next_token belongs to another process"}
    ]


def test_a_next_token_must_have_its_listings_key_shape(api: ApiCall) -> None:
    """A token that decodes but isn't a key of this listing is a 422, not a DynamoDB error."""
    from entcommon.paging import encode_token

    pid = new_process(api)
    good = {"upload_id": "upl_1", "process_id": pid, "created_at": "2026-10-11T00:00:00.000Z"}
    for key in ({"process_id": pid}, good | {"extra": "x"}):
        resp = api("GET", f"/v1/processes/{pid}/uploads", query={"next_token": encode_token(key)})

        assert resp.status == 422
        assert resp.body["errors"] == [
            {"loc": ["query", "next_token"], "msg": "next_token is not valid"}
        ]
    ok = api("GET", f"/v1/processes/{pid}/uploads", query={"next_token": encode_token(good)})
    assert ok.status == 200


@pytest.mark.parametrize(
    "query",
    [{"limit": "0"}, {"limit": "501"}, {"limit": "x"}, {"next_token": "!!!"}, {"other": "1"}],
)
def test_uploads_query_validation(api: ApiCall, query: dict[str, str]) -> None:
    pid = new_process(api)

    resp = api("GET", f"/v1/processes/{pid}/uploads", query=query)

    assert resp.status == 422
    assert resp.body["errors"][0]["loc"][0] == "query"


def test_uploads_are_read_like_the_process(api: ApiCall) -> None:
    pid = new_process(api)
    ticket(api, pid)

    assert len(uploads(api, pid, caller=READER_ARN)) == 1
    assert len(uploads(api, pid, caller=ADMIN_ARN)) == 1
    # Another source's caller can't tell the process exists, as for GET /v1/processes/{id}.
    denied = api("GET", f"/v1/processes/{pid}/uploads", caller=OTHER_ARN)
    assert (denied.status, denied.body) == (404, {"message": f"process {pid} not found"})


def test_uploads_of_an_unknown_process_is_404(api: ApiCall) -> None:
    assert api("GET", "/v1/processes/prc_missing/uploads").status == 404


def test_a_process_without_uploads_has_an_empty_page(api: ApiCall) -> None:
    pid = new_process(api)

    resp = api("GET", f"/v1/processes/{pid}/uploads")

    assert resp.body == {"items": [], "next_token": None}
