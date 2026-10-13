"""Settlement, ordering, deletes and the janitor."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import boto3
import pytest

from tests.conftest import BUCKET, REGION, ApiCall
from tests.test_registrar import (
    PDF,
    article,
    deliver,
    key_of,
    new_process,
    process,
    put_with_ticket,
    s3_event,
    ticket,
    upload,
)

PDF2 = b"%PDF-1.7 the second version"


def close(api: ApiCall, pid: str, **body: Any) -> dict[str, Any]:
    resp = api("POST", f"/v1/processes/{pid}/close", body or None)
    assert resp.status == 200, resp.body
    return dict(resp.body)


def settled(api: ApiCall, pid: str) -> bool:
    return bool(api("GET", f"/v1/processes/{pid}").body["settled"])


# -- settlement ------------------------------------------------------------------------------


def test_a_superseded_upload_still_settles_its_process(api: ApiCall) -> None:
    pid = new_process(api)
    first, second = ticket(api, pid, PDF), ticket(api, pid, PDF2)  # same article, twice
    put_with_ticket(second, PDF2)
    deliver(s3_event(key_of(second)))
    put_with_ticket(first, PDF)
    deliver(s3_event(key_of(first)))  # older ticket arrives last: superseded

    body = close(api, pid)

    assert upload(first["upload_id"])["status"] == "SUPERSEDED"
    assert (body["expected_count"], body["tickets_issued"], body["pending_uploads"]) == (2, 2, 0)
    assert (body["registered_count"], body["superseded_count"]) == (1, 1)
    assert body["settled"]


def test_a_close_without_count_waits_for_uploads_still_on_their_way(api: ApiCall) -> None:
    pid = new_process(api)
    t = ticket(api, pid)

    body = close(api, pid)  # the object hasn't arrived yet

    assert (body["expected_count"], body["pending_uploads"], body["settled"]) == (1, 1, False)
    put_with_ticket(t)
    deliver(s3_event(key_of(t)))
    assert settled(api, pid)


def test_a_rejected_upload_settles_too(api: ApiCall) -> None:
    pid = new_process(api)
    t = ticket(api, pid)
    s3().put_object(Bucket=BUCKET, Key=key_of(t), Body=b"not the declared bytes")
    deliver(s3_event(key_of(t)))

    body = close(api, pid)

    assert (body["rejected_count"], body["pending_uploads"], body["settled"]) == (1, 0, True)


def test_no_tickets_after_close(api: ApiCall) -> None:
    pid = new_process(api)
    close(api, pid)

    resp = api("POST", f"/v1/processes/{pid}/uploads", {
        "filename": "a.pdf", "content_type": "application/pdf", "size_bytes": 3,
        "sha256": "0" * 64,
    })  # fmt: skip

    assert resp.status == 409


# -- ordering and deletes --------------------------------------------------------------------


def test_the_later_ticket_wins_whatever_order_the_objects_arrive_in(api: ApiCall) -> None:
    pid = new_process(api)
    first, second = ticket(api, pid, PDF), ticket(api, pid, PDF2)
    assert (
        int(upload(first["upload_id"])["upload_seq"]),
        int(upload(second["upload_id"])["upload_seq"]),
    ) == (1, 2)

    put_with_ticket(second, PDF2)
    deliver(s3_event(key_of(second)))
    put_with_ticket(first, PDF)
    deliver(s3_event(key_of(first)))

    row = article(second["article_id"])
    assert row is not None
    assert (row["upload_id"], int(row["upload_seq"])) == (second["upload_id"], 2)


def test_a_deleted_article_isnt_revived_by_an_older_upload(api: ApiCall) -> None:
    pid = new_process(api)
    old, current = ticket(api, pid, PDF), ticket(api, pid, PDF2)
    put_with_ticket(current, PDF2)
    deliver(s3_event(key_of(current)))
    assert api("DELETE", f"/v1/articles/{current['article_id']}").status == 204

    put_with_ticket(old, PDF)
    deliver(s3_event(key_of(old)))  # a late event for an upload from before the delete

    assert article(current["article_id"]) is None
    assert upload(old["upload_id"])["status"] == "SUPERSEDED"


def test_a_new_upload_after_a_delete_brings_the_article_back(api: ApiCall) -> None:
    pid = new_process(api)
    t = ticket(api, pid)
    put_with_ticket(t)
    deliver(s3_event(key_of(t)))
    api("DELETE", f"/v1/articles/{t['article_id']}")

    again = ticket(api, new_process(api), PDF2)
    put_with_ticket(again, PDF2)
    deliver(s3_event(key_of(again)))

    row = article(t["article_id"])
    assert row is not None
    assert row["upload_id"] == again["upload_id"]


def test_purge_erases_every_stored_version(api: ApiCall) -> None:
    pid = new_process(api)
    first = ticket(api, pid, PDF)
    put_with_ticket(first, PDF)
    deliver(s3_event(key_of(first)))
    second = ticket(api, pid, PDF2)
    put_with_ticket(second, PDF2)
    deliver(s3_event(key_of(second)))

    resp = api("DELETE", f"/v1/articles/{first['article_id']}", query={"purge": "true"})

    assert resp.status == 204
    left = s3().list_object_versions(Bucket=BUCKET, Prefix=f"raw/{first['article_id']}/")
    assert not left.get("Versions")
    assert not left.get("DeleteMarkers")


def test_unused_objects_are_tagged_for_expiry(api: ApiCall) -> None:
    pid = new_process(api)
    t = ticket(api, pid)
    s3().put_object(Bucket=BUCKET, Key=key_of(t), Body=b"tampered")
    deliver(s3_event(key_of(t)))

    tags = s3().get_object_tagging(Bucket=BUCKET, Key=key_of(t))["TagSet"]

    assert tags == [{"Key": "producer-unused", "Value": "rejected"}]


# -- listing ---------------------------------------------------------------------------------


def test_a_process_keeps_listing_articles_that_moved_on(api: ApiCall) -> None:
    p1 = new_process(api)
    t = ticket(api, p1)
    put_with_ticket(t)
    deliver(s3_event(key_of(t)))
    p2 = new_process(api)
    again = ticket(api, p2, PDF2)
    put_with_ticket(again, PDF2)
    deliver(s3_event(key_of(again)))  # the article's process_id is p2 now

    listed = api("GET", f"/v1/processes/{p1}/articles").body["items"]

    assert [a["article_id"] for a in listed] == [t["article_id"]]


# -- janitor ---------------------------------------------------------------------------------


@pytest.fixture
def janitor(api: ApiCall, monkeypatch: pytest.MonkeyPatch) -> Any:
    from producer.config import get_settings
    from producer.janitor import Janitor
    from producer.repository import Repository

    sqs = boto3.client("sqs", region_name=REGION)
    url = sqs.create_queue(QueueName="registrar")["QueueUrl"]
    monkeypatch.setenv("REGISTRAR_QUEUE_URL", url)
    get_settings.cache_clear()
    settings = get_settings()
    repo = Repository.from_names(
        settings.process_table, settings.articles_table, settings.uploads_table
    )
    return Janitor(settings, repo, s3(), sqs), sqs, url


def later(hours: float) -> Any:
    from producer.timeutil import utcnow

    return utcnow() + timedelta(hours=hours)


def test_janitor_expires_tickets_that_were_never_used(api: ApiCall, janitor: Any) -> None:
    jan, _, _ = janitor
    pid = new_process(api)
    t = ticket(api, pid)
    close(api, pid)

    assert jan.run(later(0.5)) == {}  # within the grace period
    counts = jan.run(later(2))

    assert counts == {"expired": 1}
    assert upload(t["upload_id"])["status"] == "EXPIRED"
    assert "pending" not in upload(t["upload_id"])
    p = process(pid)
    assert (int(p["pending_uploads"]), int(p["expired_count"])) == (0, 1)
    assert settled(api, pid)


def test_the_ticket_grace_period_is_configurable(api: ApiCall, janitor: Any) -> None:
    from dataclasses import replace

    from producer.janitor import Janitor

    jan, _, _ = janitor
    quick = Janitor(replace(jan.settings, ticket_grace_seconds=60), jan.repo, jan.s3, jan.sqs)
    t = ticket(api, new_process(api))

    assert quick.run(later(0.5)) == {"expired": 1}  # the 15-minute URL plus a minute
    assert upload(t["upload_id"])["status"] == "EXPIRED"


def test_janitor_resends_objects_whose_event_was_lost(api: ApiCall, janitor: Any) -> None:
    jan, sqs, url = janitor
    pid = new_process(api)
    t = ticket(api, pid)
    put_with_ticket(t)  # the object is there, but the registrar never heard of it

    assert jan.run(later(2)) == {"resent": 1}
    assert jan.run(later(3)) == {}  # once

    messages = sqs.receive_message(QueueUrl=url)["Messages"]
    deliver(json.loads(messages[0]["Body"]))
    assert upload(t["upload_id"])["status"] == "REGISTERED"


def test_janitor_closes_idle_open_processes(api: ApiCall, janitor: Any) -> None:
    jan, _, _ = janitor
    pid = new_process(api)
    t = ticket(api, pid)
    put_with_ticket(t)
    deliver(s3_event(key_of(t)))

    counts = jan.run(later(24 * 8))

    assert counts["processes_closed"] == 1
    body = api("GET", f"/v1/processes/{pid}").body
    assert (body["status"], body["expected_count"], body["settled"]) == ("CLOSED", 1, True)


def s3() -> Any:
    return boto3.client("s3", region_name=REGION)
