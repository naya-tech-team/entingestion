"""Phase 2 operations in process: resume, re-queue, the control API and the reconciler
(brief §10-11), on top of the pipeline harness in test_pipeline."""

from __future__ import annotations

import json
from typing import Any

import boto3
import pytest

from ingest import aws, requeue
from ingest.models.status import ArticleStatus, StepName
from ingest.resume import resume_stage
from ingest.staging.io import parse_s3_uri
from ingest.status import keys
from ingest.status.store import StepRef
from ingest.timeutil import iso_at, now_epoch
from tests.conftest import REGION
from tests.unit.test_pipeline import (
    PAGE,
    FakeContext,
    FakeTitan,
    FakeVectorStore,
    latest,
    message_from,
    produce,
    run,
    summary,
)

Env = tuple[FakeVectorStore, FakeTitan]
USER = "arn:aws:iam::123456789012:user/operator"


def queued() -> list[dict[str, Any]]:
    sqs = boto3.client("sqs", region_name=REGION)
    url = aws.get_settings().ingest_queue_url
    out: list[dict[str, Any]] = []
    while True:
        resp = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10)
        messages = resp.get("Messages", [])
        if not messages:
            return out
        for m in messages:
            out.append(json.loads(m["Body"]))
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])


def failed_at_embed() -> None:
    run(message_from(produce("art_1", PAGE)), fail_at=StepName.EMBED)
    assert latest("art_1")["status"] == ArticleStatus.FAILED_PERMANENT


def step_item(step: StepName) -> dict[str, Any]:
    item = aws.status_store().get(keys.step_key("art_1", str(latest("art_1")["run_id"]), step))
    assert item is not None
    return item


def settle(process_id: str, registered: int) -> None:
    boto3.resource("dynamodb", region_name=REGION).Table("process").put_item(
        Item={
            "process_id": process_id,
            "status": "CLOSED",
            "expected_count": registered,
            "registered_count": registered,
            "rejected_count": 0,
        }
    )


# -- roll-up ---------------------------------------------------------------------------------


def test_unchanged_rerun_keeps_the_articles_count_in_its_process(env: Env) -> None:
    run(message_from(produce("art_1", PAGE)))
    # A metadata PATCH: same content, same process, new updated_at.
    patched = produce("art_1", PAGE, updated_at="2026-10-11T01:00:00.000Z")
    assert run(message_from(patched))[1]["action"] == "SKIP"
    assert summary("prc_A") == {"indexed": 1, "skipped": 0, "failed": 0}

    # The same content joining another process is counted there as skipped (UC3).
    other = produce("art_1", PAGE, process_id="prc_B", updated_at="2026-10-11T02:00:00.000Z")
    run(message_from(other))
    assert summary("prc_B") == {"indexed": 0, "skipped": 1, "failed": 0}


def test_completion_follows_the_counts_after_a_replay(env: Env) -> None:
    store = aws.status_store()
    settle("prc_A", 1)
    failed_at_embed()
    assert (store.summary("prc_A") or {})["status"] == "COMPLETE_WITH_ERRORS"  # set by MarkFailed
    assert store.open_summaries() == []

    message = requeue.message_for(store, "art_1", trigger="MANUAL", resume=True)
    assert message is not None
    run(message.model_dump(mode="json"))

    assert summary("prc_A") == {"indexed": 1, "skipped": 0, "failed": 0}
    assert (store.summary("prc_A") or {})["status"] == "COMPLETE"


# -- resume ----------------------------------------------------------------------------------


def test_resume_starts_after_the_last_intact_staged_output(env: Env) -> None:
    failed_at_embed()
    store = aws.status_store()
    sha = latest("art_1")["content_sha256"]
    assert resume_stage(store, latest("art_1"), sha) == StepName.EMBED

    bucket, key = parse_s3_uri(str(step_item(StepName.CHUNK)["output_uri"]))
    boto3.client("s3", region_name=REGION).delete_object(Bucket=bucket, Key=key)
    assert resume_stage(store, latest("art_1"), sha) == StepName.CHUNK
    assert resume_stage(store, latest("art_1"), "0" * 64) is None  # other content: from Fetch


def test_resume_after_fetch_starts_at_extract(env: Env) -> None:
    # Fetch stages nothing: Extract re-reads (and re-verifies) the producer's versioned object.
    run(message_from(produce("art_1", PAGE)), fail_at=StepName.EXTRACT)
    sha = latest("art_1")["content_sha256"]
    assert resume_stage(aws.status_store(), latest("art_1"), sha) == StepName.EXTRACT


def test_resumed_replay_reruns_only_the_rest_and_fixes_the_count(env: Env) -> None:
    vectors, _ = env
    failed_at_embed()
    message = requeue.message_for(aws.status_store(), "art_1", trigger="MANUAL", resume=True)
    assert message is not None
    assert message.from_stage == StepName.EMBED

    run_id, outcome = run(message.model_dump(mode="json"))

    assert outcome["action"] == "DONE"
    assert latest("art_1")["status"] == "INDEXED"
    steps = [i["sk"] for i in aws.status_store().run_items("art_1", run_id) if "#STEP#" in i["sk"]]
    assert [s.rsplit("_", 1)[-1] for s in steps] == ["EMBED", "VALIDATE", "INDEX", "FINALIZE"]
    assert summary("prc_A") == {"indexed": 1, "skipped": 0, "failed": 0}
    assert vectors.active("art_1")


def test_message_for_an_indexed_article_is_a_plain_run(env: Env) -> None:
    run(message_from(produce("art_1", PAGE)))
    message = requeue.message_for(aws.status_store(), "art_1", trigger="MANUAL", resume=True)
    assert message is not None
    assert (message.force, message.from_stage) == (False, None)


def test_message_for_deleted_and_unknown_articles(env: Env) -> None:
    run(message_from(produce("art_1", PAGE)))
    boto3.resource("dynamodb", region_name=REGION).Table("articles").delete_item(
        Key={"article_id": "art_1"}
    )
    store = aws.status_store()
    message = requeue.message_for(store, "art_1", trigger="RECONCILER")
    assert message is not None
    assert message.event == "REMOVE"
    assert requeue.message_for(store, "art_nope", trigger="MANUAL") is None


def test_only_stream_deliveries_dedupe_by_name(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from ingest import dispatcher

    names: list[str] = []

    class FakeSfn:
        def start_execution(self, stateMachineArn: str, name: str, input: str) -> dict[str, str]:
            names.append(name)
            return {"executionArn": name}

    monkeypatch.setattr(aws, "sfn", FakeSfn)
    record = produce("art_1", PAGE)
    stream = dispatcher.to_message(record)
    manual = stream.model_copy(update={"trigger": "MANUAL"})
    for message in (stream, stream, manual, manual):
        dispatcher.start(message)

    assert names[0] == names[1]  # same stream record: same name (DUP)
    assert len({names[0], names[2], names[3]}) == 3


# -- control API -----------------------------------------------------------------------------


def api(method: str, path: str, body: Any = None, caller: str = USER) -> tuple[int, Any]:
    """Call the control API handler; a query string in `path` becomes its parameters."""
    from urllib.parse import parse_qsl

    from ingest.control_api.app import handler

    path, _, query = path.partition("?")
    event = {
        "resource": "/{proxy+}",
        "path": path,
        "httpMethod": method,
        "headers": {"Content-Type": "application/json"},
        "multiValueHeaders": {},
        "queryStringParameters": dict(parse_qsl(query)) or None,
        "multiValueQueryStringParameters": None,
        "pathParameters": {"proxy": path.lstrip("/")},
        "stageVariables": None,
        "requestContext": {
            "requestId": "req-1",
            "stage": "prod",
            "resourcePath": "/{proxy+}",
            "httpMethod": method,
            "path": f"/prod{path}",
            "identity": {"userArn": caller, "caller": "operator"},
        },
        "body": None if body is None else json.dumps(body),
        "isBase64Encoded": False,
    }
    resp = handler(event, FakeContext())  # type: ignore[arg-type]
    return int(resp["statusCode"]), json.loads(resp["body"])


@pytest.mark.parametrize(
    "body",
    [None, {}, {"article_ids": []}, {"article_ids": ["a"], "nope": 1}, {"article_ids": ["a"],
     "from_stage": "WRONG"}],
)  # fmt: skip
def test_ingest_rejects_bad_requests(env: Env, body: Any) -> None:
    status, out = api("POST", "/v1/ingest", body)
    assert status == 422
    assert out["errors"]


def test_ingest_queues_manual_reruns(env: Env) -> None:
    failed_at_embed()

    status, out = api("POST", "/v1/ingest", {"article_ids": ["art_1", "art_x"], "dry_run": True})
    assert status == 200
    assert out["messages"][0]["from_stage"] == "EMBED"
    assert out["not_found"] == ["art_x"]
    assert queued() == []

    status, out = api("POST", "/v1/ingest", {"article_ids": ["art_1"]})
    assert (status, out["queued"]) == (202, 1)
    [message] = queued()
    assert message["trigger"] == "MANUAL"
    assert message["requested_by"] == USER
    assert message["from_stage"] == "EMBED"


def test_process_ingest_only_failed(env: Env) -> None:
    failed_at_embed()
    run(message_from(produce("art_2", PAGE + " ")))

    status, out = api("POST", "/v1/processes/prc_A/ingest", {"only_failed": True})
    assert (status, out["queued"]) == (202, 1)
    assert [m["article_id"] for m in queued()] == ["art_1"]


def test_redrive_by_error_code(env: Env) -> None:
    failed_at_embed()

    status, out = api("POST", "/v1/dlq/redrive", {"error_code": "E_OTHER"})
    assert (status, out["queued"]) == (202, 0)
    status, out = api("POST", "/v1/dlq/redrive", {"error_code": "E_UNREADABLE"})
    assert (status, out["queued"]) == (202, 1)
    assert "dlq" not in out


def test_redrive_without_a_code_also_reports_the_dlq(env: Env) -> None:
    status, out = api("POST", "/v1/dlq/redrive", {"dry_run": True})
    assert status == 200
    assert out["dlq"] == {"messages": 0, "moved": False}


def test_article_status(env: Env) -> None:
    assert api("GET", "/v1/articles/art_1/status")[0] == 404
    failed_at_embed()

    status, out = api("GET", "/v1/articles/art_1/status")

    assert status == 200
    assert out["latest"]["status"] == "FAILED_PERMANENT"
    assert len(out["runs"]) == 1
    assert out["steps"][-1]["sk"].endswith("EMBED")
    assert out["execution"] is None  # the harness has no real execution ARN


def test_process_summary(env: Env) -> None:
    assert api("GET", "/v1/processes/prc_A/summary")[0] == 404
    failed_at_embed()
    run(message_from(produce("art_2", PAGE + " ")))
    settle("prc_A", 2)

    status, out = api("GET", "/v1/processes/prc_A/summary")

    assert status == 200
    assert out["counts"] == {"indexed": 1, "skipped": 0, "failed": 1}
    assert out["producer"]["registered_count"] == 2
    assert out["failures_by_error_code"] == {"E_UNREADABLE": 1}


def test_process_articles(env: Env) -> None:
    assert api("GET", "/v1/processes/prc_A/articles") == (200, {"process_id": "prc_A", "items": []})
    run(message_from(produce("art_2", PAGE + " ")))
    failed_at_embed()

    status, out = api("GET", "/v1/processes/prc_A/articles")

    assert status == 200
    assert out["process_id"] == "prc_A"
    failed, indexed = out["items"]  # in article_id order
    assert set(indexed) == {
        "article_id", "status", "stage", "doc_version", "chunk_count", "error_code", "error_msg",
        "updated_at", "indexed_at", "tenant_id",
    }  # fmt: skip
    assert (indexed["article_id"], indexed["status"]) == ("art_2", "INDEXED")
    assert isinstance(indexed["chunk_count"], int)
    assert indexed["chunk_count"] > 0
    assert indexed["indexed_at"] is not None
    assert indexed["error_code"] is None
    assert (failed["article_id"], failed["status"]) == ("art_1", "FAILED_PERMANENT")
    assert failed["error_code"] == "E_UNREADABLE"
    assert api("GET", "/v1/processes/prc_B/articles")[1]["items"] == []


def test_unknown_routes(env: Env) -> None:
    assert api("GET", "/v1/nope")[0] == 404


def put_latest(article_id: str, status: str, at: str, **fields: Any) -> None:
    """A LATEST item in `status` since `at`, as the status store writes it."""
    boto3.resource("dynamodb", region_name=REGION).Table("ingestion_status").put_item(
        Item={"pk": article_id, "sk": keys.LATEST, "status": status, "gsi1pk": status,
              "gsi1sk": at, "updated_at": at, "doc_version": 2, "process_id": "prc_A",
              "tenant_id": "t1", **fields}
    )  # fmt: skip


def test_ops_overview(env: Env) -> None:
    put_latest("art_a", "FAILED_PERMANENT", "2026-10-11T02:00:00.000Z", error_code="E_X")
    put_latest("art_b", "FAILED_PERMANENT", "2026-10-11T01:00:00.000Z", error_code="E_X")
    put_latest("art_c", "EMBEDDING", "2026-10-11T03:00:00.000Z")
    put_latest("art_d", "INDEXED", "2026-10-11T00:00:00.000Z")
    sqs = boto3.client("sqs", region_name=REGION)
    sqs.send_message(QueueUrl=aws.get_settings().ingest_dlq_url, MessageBody="{}")
    sqs.send_message(QueueUrl=aws.get_settings().ingest_queue_url, MessageBody="{}")
    sqs.receive_message(QueueUrl=aws.get_settings().ingest_queue_url, VisibilityTimeout=60)

    status, out = api("GET", "/v1/ops/overview")

    assert status == 200
    assert set(out) == {"statuses", "queues", "generated_at"}
    assert set(out["statuses"]) == {
        "CLAIMED", "FETCHING", "EXTRACTING", "NORMALIZING", "CHUNKING", "EMBEDDING", "VALIDATING",
        "STAGED", "INDEXING", "FAILED_RETRYABLE", "FAILED_PERMANENT",
    }  # fmt: skip
    assert out["statuses"]["FAILED_PERMANENT"] == {
        "count": 2,
        "oldest_at": "2026-10-11T01:00:00.000Z",
        "oldest_article_id": "art_b",
    }
    assert out["statuses"]["EMBEDDING"]["count"] == 1
    assert out["statuses"]["CLAIMED"] == {"count": 0, "oldest_at": None, "oldest_article_id": None}
    # The test environment has no backfill DLQ, so it's left out.
    assert out["queues"] == {
        "ingest": {"visible": 0, "in_flight": 1},
        "ingest_dlq": {"visible": 1, "in_flight": 0},
        "backfill": {"visible": 0, "in_flight": 0},
    }


def test_ops_overview_tolerates_an_unreadable_queue(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BACKFILL_DLQ_URL", f"https://sqs.{REGION}.amazonaws.com/123456789012/gone")
    aws.get_settings.cache_clear()

    status, out = api("GET", "/v1/ops/overview")

    assert status == 200
    assert out["queues"]["backfill_dlq"] == {"error": "unavailable"}
    assert out["queues"]["ingest"] == {"visible": 0, "in_flight": 0}


def test_articles_by_status_pages_oldest_first(env: Env) -> None:
    for n in range(3):
        put_latest(
            f"art_{n}", "FAILED_PERMANENT", f"2026-10-11T0{3 - n}:00:00.000Z",
            error_code="E_X", error_msg="boom", stage="EMBED",
        )  # fmt: skip
    put_latest("art_ok", "INDEXED", "2026-10-11T00:00:00.000Z")

    status, out = api("GET", "/v1/articles?status=FAILED_PERMANENT&limit=2")

    assert status == 200
    assert [i["article_id"] for i in out["items"]] == ["art_2", "art_1"]
    assert out["items"][0] == {
        "article_id": "art_2", "status": "FAILED_PERMANENT", "stage": "EMBED",
        "process_id": "prc_A", "tenant_id": "t1", "doc_version": 2, "error_code": "E_X",
        "error_msg": "boom", "updated_at": "2026-10-11T01:00:00.000Z",
    }  # fmt: skip
    assert out["next_token"]

    status, out = api(
        "GET", f"/v1/articles?status=FAILED_PERMANENT&limit=2&next_token={out['next_token']}"
    )
    assert status == 200
    assert [i["article_id"] for i in out["items"]] == ["art_0"]
    assert out["next_token"] is None

    status, out = api("GET", "/v1/articles?status=CLAIMED")  # default limit
    assert (status, out) == (200, {"items": [], "next_token": None})


@pytest.mark.parametrize(
    "query",
    ["", "status=", "status=NOPE", "status=FAILED_PERMANENT&limit=0",
     "status=FAILED_PERMANENT&limit=201", "status=FAILED_PERMANENT&limit=ten",
     "status=FAILED_PERMANENT&next_token=%%%", "status=FAILED_PERMANENT&next_token=bm90IGpzb24",
     "status=FAILED_PERMANENT&next_token=WzFd"],
)  # fmt: skip
def test_articles_by_status_rejects_bad_queries(env: Env, query: str) -> None:
    status, out = api("GET", f"/v1/articles?{query}")
    assert status == 422
    assert out["errors"]


def test_articles_by_status_limit_bounds_and_foreign_tokens(env: Env) -> None:
    for n in range(2):
        put_latest(f"art_{n}", "FAILED_RETRYABLE", f"2026-10-11T0{n}:00:00.000Z")
    assert len(api("GET", "/v1/articles?status=FAILED_RETRYABLE&limit=1")[1]["items"]) == 1
    assert len(api("GET", "/v1/articles?status=FAILED_RETRYABLE&limit=200")[1]["items"]) == 2

    token = api("GET", "/v1/articles?status=FAILED_RETRYABLE&limit=1")[1]["next_token"]
    status, out = api("GET", f"/v1/articles?status=FAILED_PERMANENT&next_token={token}")
    assert status == 422
    assert out["errors"][0]["msg"] == "next_token belongs to another status"


# -- reconciler ------------------------------------------------------------------------------


def reconcile(now: int | None = None) -> dict[str, int]:
    from ingest.reconciler.app import Reconciler

    return Reconciler(aws.status_store(), now or now_epoch(), 500).run()


def claim_only(article_id: str = "art_1") -> str:
    from ingest.ids import new_run_id
    from ingest.steps import claim

    message = message_from(produce(article_id, PAGE))
    ctx = claim.handler({"ctx": {"message": message, "run_id": new_run_id()}}, FakeContext())
    return str(ctx["run_id"])


def test_reconciler_requeues_expired_leases(env: Env) -> None:
    claim_only()
    assert reconcile()["requeued"] == 0  # the lease is live

    counts = reconcile(now_epoch() + 1000)

    assert counts["requeued_lease"] == 1
    [message] = queued()
    assert (message["trigger"], message["force"]) == ("RECONCILER", True)


def test_reconciler_retries_then_escalates(env: Env) -> None:
    store = aws.status_store()
    run_id = claim_only()
    ref = StepRef("art_1", run_id, "prc_A")
    store.mark_retryable(ref, "E_THROTTLED", "slow down", now_epoch() - 1)

    assert reconcile()["requeued_retryable"] == 1
    assert len(queued()) == 1

    store.table.update_item(
        Key=keys.latest_key("art_1"), UpdateExpression="SET retry_count = :n",
        ExpressionAttributeValues={":n": 5},
    )  # fmt: skip
    counts = reconcile()

    assert counts["escalated"] == 1
    assert latest("art_1")["status"] == "FAILED_PERMANENT"
    assert summary("prc_A")["failed"] == 1
    assert queued() == []


def test_reconciler_sends_stuck_staged_articles_to_index(env: Env) -> None:
    claim_only()
    old = iso_at(now_epoch() - 3600)
    aws.status_store().table.update_item(
        Key=keys.latest_key("art_1"),
        UpdateExpression="SET #s = :st, gsi1pk = :st, gsi1sk = :old REMOVE gsi3pk, gsi3sk, "
        "lease_expires_at, lease_owner",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":st": "STAGED", ":old": old},
    )

    assert reconcile()["requeued_staged"] == 1
    assert queued()[0]["from_stage"] == "INDEX"


def test_reconciler_enqueues_articles_the_stream_missed(env: Env) -> None:
    now = now_epoch()
    produce("art_1", PAGE, updated_at=iso_at(now - 600))  # never reached ingestion
    produce("art_2", PAGE + " ", updated_at=iso_at(now - 30))  # may still be on its way
    run(message_from(produce("art_3", PAGE + "  ", updated_at=iso_at(now - 600))))  # in sync

    counts = reconcile(now)

    assert counts["requeued_drift"] == 1
    [message] = queued()
    assert (message["article_id"], message["trigger"]) == ("art_1", "RECONCILER")


def test_reconciler_completes_settled_processes(env: Env) -> None:
    run(message_from(produce("art_1", PAGE)))  # no producer process yet: can't complete
    store = aws.status_store()
    assert [s["pk"] for s in store.open_summaries()] == ["PROCESS#prc_A"]

    settle("prc_A", 1)
    counts = reconcile()

    assert counts["processes_completed"] == 1
    assert (store.summary("prc_A") or {})["status"] == "COMPLETE"
    assert store.open_summaries() == []


def test_reconciler_queues_deletes_left_pending(env: Any) -> None:
    from ingest.models import IngestMessage
    from ingest.reconciler.app import Reconciler
    from ingest.status import keys as status_keys

    store = aws.status_store()
    store.claim(
        IngestMessage(
            article_id="art_p",
            process_id="prc_A",
            event="UPSERT",
            s3_uri="s3://raw/raw/art_p/u/doc.html",
            content_sha256="1" * 64,
            trigger="STREAM",
        ),
        "run_a",
        None,
    )
    store.claim(IngestMessage(article_id="art_p", event="REMOVE", trigger="STREAM"), "run_b", None)
    store.table.update_item(  # the running run crashed: its lease ran out without a handover
        Key=status_keys.latest_key("art_p"),
        UpdateExpression="SET lease_expires_at = :t",
        ExpressionAttributeValues={":t": 1},
    )

    counts = Reconciler(store, now_epoch(), 500).run()

    assert counts["requeued_pending_delete"] == 1
    assert [(m["article_id"], m["event"]) for m in queued()] == [("art_p", "REMOVE")]


def test_weekly_sweep_deletes_articles_whose_producer_row_is_gone(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingest.reconciler.app import Reconciler
    from tests.unit.test_quality import FakeLambda

    run(message_from(produce("art_keep", PAGE)))
    run(message_from(produce("art_lost", PAGE + " ")))
    boto3.resource("dynamodb", region_name=REGION).Table("articles").delete_item(
        Key={"article_id": "art_lost"}
    )  # the REMOVE never reached us
    rows = [[a, 1, int(latest(a)["chunk_count"])] for a in ("art_keep", "art_lost")]
    monkeypatch.setattr(
        aws, "lambda_client", lambda: FakeLambda({"versions": {"rows": rows, "next": None}})
    )

    counts = Reconciler(aws.status_store(), now_epoch(), 500).run(drift=True)

    assert counts["requeued_producer_gone"] == 1
    assert {m["article_id"]: m["event"] for m in queued()}["art_lost"] == "REMOVE"


def test_producer_updates_are_found_on_every_shard_and_the_old_index(env: Any) -> None:
    from ingest import producer

    articles = boto3.resource("dynamodb", region_name=REGION).Table("articles")
    day = "2026-10-11"
    for i in range(20):  # spread over the shards
        at = f"{day}T00:00:{i:02d}.000Z"
        shard = f"{day}#{i % 16:02d}"
        articles.put_item(
            Item={"article_id": f"art_s{i}", "updated_at": at, "updated_shard": shard}
        )
    articles.put_item(Item={"article_id": "art_old", "updated_at": f"{day}T01:00:00.000Z",
                            "updated_date": day})  # fmt: skip

    found = {a["article_id"] for a in producer.updated_on(day)}

    assert found == {f"art_s{i}" for i in range(20)} | {"art_old"}


@pytest.mark.parametrize(
    ("process", "settled"),
    [
        ({"tickets_issued": 3, "pending_uploads": 0}, True),
        ({"tickets_issued": 3, "pending_uploads": 1}, False),  # an upload has no outcome yet
        ({"tickets_issued": 2, "pending_uploads": 0}, False),  # fewer uploads than expected
        ({"registered_count": 2, "rejected_count": 1}, True),  # before uploads were counted
    ],
)
def test_settled_follows_the_producer(process: dict[str, Any], settled: bool) -> None:
    from ingest.status.store import _settled

    assert _settled({"status": "CLOSED", "expected_count": 3} | process) is settled
