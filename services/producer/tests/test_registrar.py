from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any
from urllib.parse import quote_plus

import boto3
import pytest
import requests

from tests.conftest import BUCKET, REGION, ApiCall, FakeContext

PDF = b"%PDF-1.7 registrar test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def table(name: str) -> Any:
    return boto3.resource("dynamodb", region_name=REGION).Table(name)


def s3() -> Any:
    return boto3.client("s3", region_name=REGION)


def new_process(api: ApiCall, source: str = "news", **body: Any) -> str:
    resp = api("POST", "/v1/processes", {"name": "p", "source": source, **body})
    assert resp.status == 201
    return str(resp.body["process_id"])


def ticket(
    api: ApiCall, pid: str, data: bytes = PDF, filename: str = "doc.pdf", **body: Any
) -> dict[str, Any]:
    payload = {
        "filename": filename,
        "content_type": "application/pdf",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "external_id": "doc-1",
        **body,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    resp = api("POST", f"/v1/processes/{pid}/uploads", payload)
    assert resp.status == 201, resp.body
    return dict(resp.body)


def key_of(t: dict[str, Any], filename: str = "doc.pdf") -> str:
    return f"raw/{t['article_id']}/{t['upload_id']}/{filename}"


def put_with_ticket(t: dict[str, Any], data: bytes = PDF) -> None:
    resp = requests.put(t["url"], data=data, headers=t["headers"], timeout=10)
    assert resp.status_code == 200, resp.text


def s3_event(key: str, bucket: str = BUCKET, version_id: str | None = None) -> dict[str, Any]:
    obj: dict[str, Any] = {"key": quote_plus(key), "size": 1}
    if version_id:
        obj["versionId"] = version_id
    return {
        "Records": [
            {
                "eventVersion": "2.1",
                "eventSource": "aws:s3",
                "awsRegion": REGION,
                "eventName": "ObjectCreated:Put",
                "s3": {"bucket": {"name": bucket}, "object": obj},
            }
        ]
    }


def deliver(*bodies: dict[str, Any]) -> dict[str, Any]:
    """Invoke the registrar with one SQS message per body; returns the partial batch response."""
    from producer.registrar import lambda_handler

    records = [
        {
            "messageId": str(uuid.uuid4()),
            "receiptHandle": "rh",
            "body": json.dumps(body),
            "attributes": {"ApproximateReceiveCount": "1"},
            "messageAttributes": {},
            "md5OfBody": "",
            "eventSource": "aws:sqs",
            "eventSourceARN": f"arn:aws:sqs:{REGION}:123456789012:registrar",
            "awsRegion": REGION,
        }
        for body in bodies
    ]
    return dict(lambda_handler({"Records": records}, FakeContext()))  # type: ignore[arg-type]


def process(pid: str) -> dict[str, Any]:
    return dict(table("process").get_item(Key={"process_id": pid})["Item"])


def upload(upload_id: str) -> dict[str, Any]:
    return dict(table("producer_uploads").get_item(Key={"upload_id": upload_id})["Item"])


def article(article_id: str) -> dict[str, Any] | None:
    item = table("articles").get_item(Key={"article_id": article_id}).get("Item")
    return dict(item) if item else None


def upload_and_register(api: ApiCall, pid: str, data: bytes = PDF, **body: Any) -> dict[str, Any]:
    t = ticket(api, pid, data, **body)
    put_with_ticket(t, data)
    assert deliver(s3_event(key_of(t))) == {"batchItemFailures": []}
    return t


# ---------------------------------------------------------------------------
# Happy path and idempotency
# ---------------------------------------------------------------------------


def test_register_creates_row_and_counts_once(api: ApiCall) -> None:
    pid = new_process(api)
    t = ticket(api, pid, title="Doc", metadata={"lang": "en"})
    put_with_ticket(t)

    assert article(t["article_id"]) is None  # no row before the object is verified
    assert deliver(s3_event(key_of(t))) == {"batchItemFailures": []}

    row = article(t["article_id"])
    assert row is not None
    assert row["status"] == "STAGED"
    assert row["process_id"] == pid
    assert row["first_process_id"] == pid
    assert row["upload_id"] == t["upload_id"]
    assert row["content_sha256"] == hashlib.sha256(PDF).hexdigest()
    assert int(row["size_bytes"]) == len(PDF)
    assert row["s3_key"] == key_of(t)
    assert row["s3_uri"] == f"s3://{BUCKET}/{key_of(t)}"
    assert row["s3_version_id"]
    assert row["title"] == "Doc"
    assert row["metadata"] == {"lang": "en"}
    assert row["external_id"] == "doc-1"
    assert row["updated_date"] == row["updated_at"][:10]

    assert int(process(pid)["registered_count"]) == 1
    assert upload(t["upload_id"])["status"] == "REGISTERED"


def test_duplicate_event_changes_nothing(api: ApiCall) -> None:
    pid = new_process(api)
    t = upload_and_register(api, pid)
    before = article(t["article_id"])

    assert deliver(s3_event(key_of(t)), s3_event(key_of(t))) == {"batchItemFailures": []}

    assert article(t["article_id"]) == before
    assert int(process(pid)["registered_count"]) == 1


def test_repeated_uploads_of_one_article_in_one_process_count_once(api: ApiCall) -> None:
    pid = new_process(api)
    first = upload_and_register(api, pid)
    second = upload_and_register(api, pid, PDF + b" v2")

    assert first["article_id"] == second["article_id"]
    assert article(first["article_id"])["upload_id"] == second["upload_id"]  # type: ignore[index]
    assert int(process(pid)["registered_count"]) == 1


def test_interrupted_registration_completes_on_redelivery(api: ApiCall) -> None:
    pid = new_process(api)
    t = ticket(api, pid)
    put_with_ticket(t)
    # A previous attempt claimed the upload and then crashed.
    table("producer_uploads").update_item(
        Key={"upload_id": t["upload_id"]},
        UpdateExpression="SET #s = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":r": "REGISTERING"},
    )

    assert deliver(s3_event(key_of(t))) == {"batchItemFailures": []}

    assert upload(t["upload_id"])["status"] == "REGISTERED"
    assert int(process(pid)["registered_count"]) == 1


def test_concurrent_count_conflicts_are_retried(
    api: ApiCall, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registrars counting into one process row at once get TransactionConflict; the count must
    still land (otherwise the process never settles)."""
    from botocore.exceptions import ClientError

    from producer import registrar, repository

    pid = new_process(api)
    t = ticket(api, pid)
    put_with_ticket(t)
    client = registrar.get_registrar().repo.client
    real = client.transact_write_items
    calls = [0]

    def conflicting(**kwargs: Any) -> Any:
        calls[0] += 1
        if calls[0] <= 2:
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException", "Message": "conflict"},
                    "CancellationReasons": [{"Code": "None"}, {"Code": "TransactionConflict"}],
                },
                "TransactWriteItems",
            )
        return real(**kwargs)

    monkeypatch.setattr(client, "transact_write_items", conflicting)
    monkeypatch.setattr(repository, "CONFLICT_BASE_S", 0)

    assert deliver(s3_event(key_of(t))) == {"batchItemFailures": []}

    # Two conflicts retried, then the three transactions: article, membership, outcome.
    assert calls[0] == 5
    assert upload(t["upload_id"])["status"] == "REGISTERED"
    assert int(process(pid)["registered_count"]) == 1


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def test_checksum_mismatch_is_rejected_without_a_row(api: ApiCall) -> None:
    pid = new_process(api)
    t = ticket(api, pid)
    tampered = b"%PDF-1.7 registrar tesX"  # same size, different bytes
    s3().put_object(Bucket=BUCKET, Key=key_of(t), Body=tampered, ContentType="application/pdf")

    assert deliver(s3_event(key_of(t))) == {"batchItemFailures": []}

    assert article(t["article_id"]) is None
    u = upload(t["upload_id"])
    assert u["status"] == "REJECTED"
    assert u["reason"] == "sha256_mismatch"
    assert int(process(pid)["rejected_count"]) == 1
    assert int(process(pid)["registered_count"]) == 0

    # A re-delivered event for the rejected upload doesn't count it twice.
    deliver(s3_event(key_of(t)))
    assert int(process(pid)["rejected_count"]) == 1


def test_size_mismatch_is_rejected(api: ApiCall) -> None:
    pid = new_process(api)
    t = ticket(api, pid)
    s3().put_object(Bucket=BUCKET, Key=key_of(t), Body=PDF + b"extra")

    deliver(s3_event(key_of(t)))

    u = upload(t["upload_id"])
    assert u["status"] == "REJECTED"
    assert u["reason"].startswith("size_mismatch")


def test_upload_id_metadata_mismatch_is_rejected(api: ApiCall) -> None:
    pid = new_process(api)
    t = ticket(api, pid)
    s3().put_object(Bucket=BUCKET, Key=key_of(t), Body=PDF, Metadata={"upload-id": "upl_other"})

    deliver(s3_event(key_of(t)))

    assert upload(t["upload_id"])["reason"] == "upload_id_metadata_mismatch"


def test_object_without_stored_checksum_is_stream_hashed(api: ApiCall) -> None:
    pid = new_process(api)
    t = ticket(api, pid)
    s3().put_object(Bucket=BUCKET, Key=key_of(t), Body=PDF)  # no ChecksumSHA256 stored

    assert deliver(s3_event(key_of(t))) == {"batchItemFailures": []}

    assert upload(t["upload_id"])["status"] == "REGISTERED"


def test_url_encoded_keys_are_decoded(api: ApiCall) -> None:
    pid = new_process(api)
    name = "my report+final (v2).pdf"
    t = ticket(api, pid, filename=name)
    put_with_ticket(t)

    assert deliver(s3_event(key_of(t, name))) == {"batchItemFailures": []}

    assert article(t["article_id"])["filename"] == name  # type: ignore[index]


# ---------------------------------------------------------------------------
# Articles across uploads and processes
# ---------------------------------------------------------------------------


def test_reupload_in_new_process_moves_article_and_keeps_first_process(api: ApiCall) -> None:
    first_pid = new_process(api)
    second_pid = new_process(api)
    a = upload_and_register(api, first_pid)
    b = upload_and_register(api, second_pid, PDF + b" v2")

    assert a["article_id"] == b["article_id"]
    row = article(a["article_id"])
    assert row is not None
    assert row["process_id"] == second_pid
    assert row["first_process_id"] == first_pid
    assert row["upload_id"] == b["upload_id"]
    assert int(process(first_pid)["registered_count"]) == 1
    assert int(process(second_pid)["registered_count"]) == 1


def test_out_of_order_events_supersede_the_older_upload(api: ApiCall) -> None:
    pid = new_process(api)
    older = ticket(api, pid)
    newer = ticket(api, pid, PDF + b" v2")
    put_with_ticket(older)
    put_with_ticket(newer, PDF + b" v2")

    deliver(s3_event(key_of(newer)))
    deliver(s3_event(key_of(older)))

    row = article(newer["article_id"])
    assert row is not None
    assert row["upload_id"] == newer["upload_id"]
    assert row["content_sha256"] == hashlib.sha256(PDF + b" v2").hexdigest()
    assert upload(older["upload_id"])["status"] == "SUPERSEDED"
    assert upload(newer["upload_id"])["status"] == "REGISTERED"
    assert int(process(pid)["registered_count"]) == 1


def test_metadata_and_title_kept_when_reupload_omits_them(api: ApiCall) -> None:
    pid = new_process(api)
    upload_and_register(api, pid, title="Original", metadata={"lang": "en"})
    t = upload_and_register(api, pid, PDF + b" v2")

    row = article(t["article_id"])
    assert row is not None
    assert row["metadata"] == {"lang": "en"}
    assert row["title"] == "Original"


def test_reupload_with_metadata_replaces_it(api: ApiCall) -> None:
    pid = new_process(api)
    upload_and_register(api, pid, metadata={"lang": "en"})
    t = upload_and_register(api, pid, PDF + b" v2", metadata={"lang": "fr"})

    assert article(t["article_id"])["metadata"] == {"lang": "fr"}  # type: ignore[index]


def test_no_external_id_creates_a_new_article_each_time(api: ApiCall) -> None:
    pid = new_process(api)
    a = upload_and_register(api, pid, external_id=None)
    b = upload_and_register(api, pid, external_id=None)

    assert a["article_id"] != b["article_id"]
    assert article(a["article_id"]) is not None
    assert article(b["article_id"]) is not None
    assert int(process(pid)["registered_count"]) == 2


# ---------------------------------------------------------------------------
# Process counts
# ---------------------------------------------------------------------------


def test_late_registrations_settle_a_closed_process(api: ApiCall) -> None:
    pid = new_process(api)
    a = ticket(api, pid, external_id="a")
    b = ticket(api, pid, PDF + b"b", external_id="b")
    closed = api("POST", f"/v1/processes/{pid}/close", {"expected_count": 2}).body
    assert closed["settled"] is False

    put_with_ticket(a)
    deliver(s3_event(key_of(a)))
    assert api("GET", f"/v1/processes/{pid}").body["settled"] is False

    put_with_ticket(b, PDF + b"b")
    deliver(s3_event(key_of(b)))
    final = api("GET", f"/v1/processes/{pid}").body
    assert final["registered_count"] == 2
    assert final["settled"] is True


def test_rejections_count_towards_settled(api: ApiCall) -> None:
    pid = new_process(api)
    good = ticket(api, pid, external_id="good")
    bad = ticket(api, pid, external_id="bad")
    api("POST", f"/v1/processes/{pid}/close", {"expected_count": 2})

    put_with_ticket(good)
    s3().put_object(Bucket=BUCKET, Key=key_of(bad), Body=b"x" * len(PDF))
    deliver(s3_event(key_of(good)), s3_event(key_of(bad)))

    final = api("GET", f"/v1/processes/{pid}").body
    assert (final["registered_count"], final["rejected_count"], final["settled"]) == (1, 1, True)


def test_list_articles_after_registration(api: ApiCall) -> None:
    pid = new_process(api)
    for i in range(3):
        upload_and_register(api, pid, PDF + bytes([i]), external_id=f"doc-{i}")

    page = api("GET", f"/v1/processes/{pid}/articles", query={"limit": "10"}).body

    assert len(page["items"]) == 3
    assert page["next_token"] is None


# ---------------------------------------------------------------------------
# Ignored events and failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"Service": "Amazon S3", "Event": "s3:TestEvent", "Bucket": BUCKET},
        s3_event("raw/art_x/upl_01M4KZA3BEBHXZMT4HR92B3BBC/doc.pdf", bucket="someone-else"),
        s3_event("tmp/whatever.pdf"),
        s3_event("raw/not-an-article/doc.pdf"),
        s3_event("raw/art_abc/upl_01M4KZA3BEBHXZMT4HR92B3BBC/doc.pdf"),  # unknown upload
    ],
)
def test_unknown_and_foreign_objects_are_ignored(aws: None, body: dict[str, Any]) -> None:
    assert deliver(body) == {"batchItemFailures": []}


def sqs_record(message_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "messageId": message_id,
        "receiptHandle": "rh",
        "body": json.dumps(body),
        "attributes": {"ApproximateReceiveCount": "1"},
        "messageAttributes": {},
        "md5OfBody": "",
        "eventSource": "aws:sqs",
        "eventSourceARN": f"arn:aws:sqs:{REGION}:123456789012:registrar",
        "awsRegion": REGION,
    }


def test_unexpected_error_fails_only_that_message(
    api: ApiCall, monkeypatch: pytest.MonkeyPatch
) -> None:
    from producer import registrar

    pid = new_process(api)
    good = ticket(api, pid, external_id="good")
    bad = ticket(api, pid, external_id="bad")
    put_with_ticket(good)
    put_with_ticket(bad)
    real_register = registrar.get_registrar().register

    def flaky(ref: Any) -> Any:
        if bad["upload_id"] in ref.key:
            raise RuntimeError("DynamoDB unavailable")
        return real_register(ref)

    monkeypatch.setattr(registrar.get_registrar(), "register", flaky)
    logged: list[str] = []
    monkeypatch.setattr(registrar.logger, "exception", lambda msg, **_: logged.append(msg))
    event = {
        "Records": [
            sqs_record("msg-good", s3_event(key_of(good))),
            sqs_record("msg-bad", s3_event(key_of(bad))),
        ]
    }

    resp = registrar.lambda_handler(event, FakeContext())  # type: ignore[arg-type]

    # Only the failed message goes back to SQS (and to the DLQ after 5 receives).
    assert resp == {"batchItemFailures": [{"itemIdentifier": "msg-bad"}]}
    assert logged == ["object failed; SQS will retry"]
    assert upload(good["upload_id"])["status"] == "REGISTERED"
    assert upload(bad["upload_id"])["status"] == "PENDING"


def test_whole_batch_failure_raises_so_lambda_reports_an_error(
    api: ApiCall, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aws_lambda_powertools.utilities.batch.exceptions import BatchProcessingError

    from producer import registrar

    pid = new_process(api)
    t = ticket(api, pid)
    put_with_ticket(t)

    def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("DynamoDB unavailable")

    monkeypatch.setattr(registrar.get_registrar(), "register", boom)
    event = {"Records": [sqs_record("msg-1", s3_event(key_of(t)))]}

    # SQS retries the whole batch, and the function's Errors metric (alarmed in Phase 3) fires.
    with pytest.raises(BatchProcessingError):
        registrar.lambda_handler(event, FakeContext())  # type: ignore[arg-type]
    assert upload(t["upload_id"])["status"] == "PENDING"
