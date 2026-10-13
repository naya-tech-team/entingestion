"""Per-source authorization through the API: who may write and read which source. The policy
itself (ARN matching, levels) is tested in services/common."""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import ADMIN_ARN, OTHER_ARN, READER_ARN, ApiCall
from tests.test_api import PDF, create_process, put_article, ticket_body
from tests.test_registrar import article, new_process, upload_and_register


def test_only_writers_of_a_source_create_its_processes(api: ApiCall) -> None:
    assert api("POST", "/v1/processes", {"name": "x", "source": "news"}).status == 201
    denied = api("POST", "/v1/processes", {"name": "x", "source": "news"}, caller=OTHER_ARN)
    assert denied.status == 403
    unknown = api("POST", "/v1/processes", {"name": "x", "source": "unconfigured"})
    assert unknown.status == 403


def test_another_sources_writer_cannot_see_or_change_a_process(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]

    assert api("GET", f"/v1/processes/{pid}", caller=OTHER_ARN).status == 404
    assert api("POST", f"/v1/processes/{pid}/close", caller=OTHER_ARN).status == 404
    upload = api("POST", f"/v1/processes/{pid}/uploads", ticket_body(), caller=OTHER_ARN)
    assert upload.status == 404
    assert api("GET", f"/v1/processes/{pid}/articles", caller=OTHER_ARN).status == 404


def test_readers_read_but_cannot_change(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]
    put_article("art_r", pid, "2026-10-11T00:00:00.000Z")

    assert api("GET", f"/v1/processes/{pid}", caller=READER_ARN).status == 200
    assert api("GET", "/v1/articles/art_r", caller=READER_ARN).status == 200
    assert api("PATCH", "/v1/articles/art_r", {"title": "x"}, caller=READER_ARN).status == 403
    assert api("DELETE", "/v1/articles/art_r", caller=READER_ARN).status == 403
    assert api("POST", f"/v1/processes/{pid}/close", caller=READER_ARN).status == 403


def test_another_source_cannot_patch_or_delete_an_article(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]
    put_article("art_x", pid, "2026-10-11T00:00:00.000Z")

    assert api("GET", "/v1/articles/art_x", caller=OTHER_ARN).status == 404
    assert api("PATCH", "/v1/articles/art_x", {"title": "x"}, caller=OTHER_ARN).status == 404
    assert api("DELETE", "/v1/articles/art_x", caller=OTHER_ARN).status == 404
    assert api("GET", "/v1/articles/art_x").status == 200  # still there


def test_admins_may_do_anything(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]
    put_article("art_a", pid, "2026-10-11T00:00:00.000Z")

    assert api("GET", f"/v1/processes/{pid}", caller=ADMIN_ARN).status == 200
    assert api("DELETE", "/v1/articles/art_a", caller=ADMIN_ARN).status == 204


@pytest.mark.parametrize("where", ["upload", "patch"])
def test_callers_cannot_set_the_tenant(api: ApiCall, where: str) -> None:
    pid = create_process(api)["process_id"]
    metadata: dict[str, Any] = {"tenant_id": "t-other"}
    if where == "upload":
        resp = api("POST", f"/v1/processes/{pid}/uploads", ticket_body(metadata=metadata))
    else:
        put_article("art_t", pid, "2026-10-11T00:00:00.000Z")
        resp = api("PATCH", "/v1/articles/art_t", {"metadata": metadata})
    assert resp.status == 422
    assert "reserved metadata keys: tenant_id" in str(resp.body)


def test_metadata_is_capped_in_bytes(api: ApiCall) -> None:
    pid = create_process(api)["process_id"]
    big = {f"k{i}": "é" * 2000 for i in range(10)}  # 10 pairs, ~40 KB of UTF-8

    resp = api("POST", f"/v1/processes/{pid}/uploads", ticket_body(metadata=big))

    assert resp.status == 422


def test_registered_articles_carry_the_sources_tenant(api: ApiCall) -> None:
    pid = new_process(api)
    t = upload_and_register(api, pid, PDF)

    row = article(t["article_id"])
    assert row is not None
    assert row["tenant_id"] == "t-news"
    assert api("GET", f"/v1/articles/{t['article_id']}").body["tenant_id"] == "t-news"
