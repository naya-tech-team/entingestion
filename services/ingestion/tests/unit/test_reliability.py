"""M5 reliability: admission slots (backpressure), the throttle budget, the chunk cap, the
permanent-failure digest and the backlog SLO metrics, on the pipeline harness."""

from __future__ import annotations

import json
from typing import Any

import boto3
import pytest

from ingest import aws, events, telemetry
from ingest.config import get_settings
from ingest.errors import ErrorCode, IngestPermanent, IngestStale
from ingest.ids import new_run_id
from ingest.models.status import ArticleStatus, StepName
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
)

Env = tuple[FakeVectorStore, FakeTitan]
BACKFILL_ARN = f"arn:aws:sqs:{REGION}:123456789012:backfill"
INGEST_ARN = f"arn:aws:sqs:{REGION}:123456789012:ingest"


def slot_holder(slot: str) -> str | None:
    item = aws.status_store().get(keys.slot_key(slot)) or {}
    return item.get("run_id")


class FakeSfn:
    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []

    def start_execution(self, stateMachineArn: str, name: str, input: str) -> dict[str, str]:
        self.inputs.append(json.loads(input))
        return {"executionArn": name}


@pytest.fixture
def sfn(env: Env, monkeypatch: pytest.MonkeyPatch) -> FakeSfn:
    fake = FakeSfn()
    monkeypatch.setattr(aws, "sfn", lambda: fake)
    return fake


# -- admission slots ------------------------------------------------------------------------


def test_slots_are_held_until_released_or_expired(env: Env) -> None:
    store = aws.status_store()
    held = [store.acquire_slot("live", 2, f"run_{i}") for i in range(3)]

    assert sorted(s for s in held if s) == ["live#0", "live#1"]
    assert held[2] is None  # both busy: nothing more is admitted
    assert store.acquire_slot("backfill", 1, "run_b") == "backfill#0"  # lanes are separate

    store.release_slot("live#0", "someone_else")  # only the holder releases
    assert slot_holder("live#0") is not None
    store.release_slot(held[0], "run_0")
    assert store.acquire_slot("live", 2, "run_3") == held[0]

    store.table.update_item(  # a run that died without ending: its slot's lease runs out
        Key=keys.slot_key(str(held[1])),
        UpdateExpression="SET expires_at = :t",
        ExpressionAttributeValues={":t": now_epoch() - 1},
    )
    assert store.acquire_slot("live", 2, "run_4") == held[1]


def test_the_dispatcher_starts_runs_with_a_slot(
    sfn: FakeSfn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingest import dispatcher

    monkeypatch.setenv("LIVE_SLOTS", "1")
    aws.reset()
    first = dispatcher.to_message(produce("art_1", PAGE))
    second = dispatcher.to_message(produce("art_2", PAGE + " "))

    assert dispatcher.start(first) == "started"
    ctx = sfn.inputs[0]["ctx"]
    assert ctx["slot"] == "live#0"
    assert slot_holder("live#0") == ctx["run_id"]
    with pytest.raises(dispatcher.SlotsBusy):  # the record goes back to its queue
        dispatcher.start(second)
    assert len(sfn.inputs) == 1


def test_no_limit_when_a_lane_has_no_slots(sfn: FakeSfn, monkeypatch: pytest.MonkeyPatch) -> None:
    from ingest import dispatcher

    monkeypatch.setenv("LIVE_SLOTS", "0")
    aws.reset()
    for i in range(3):
        dispatcher.start(dispatcher.to_message(produce(f"art_{i}", PAGE + " " * i)))

    assert len(sfn.inputs) == 3
    assert all("slot" not in i["ctx"] for i in sfn.inputs)


def sqs_record(body: str, source_arn: str, message_id: str) -> dict[str, Any]:
    return {
        "messageId": message_id,
        "receiptHandle": f"rh-{message_id}",
        "body": body,
        "attributes": {"ApproximateReceiveCount": "1"},
        "messageAttributes": {},
        "eventSource": "aws:sqs",
        "eventSourceARN": source_arn,
        "awsRegion": REGION,
    }


class InvocationContext(FakeContext):
    def get_remaining_time_in_millis(self) -> int:
        return 60_000


def test_a_busy_lane_hands_records_back_and_poison_goes_to_the_dlq(
    sfn: FakeSfn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingest import dispatcher

    monkeypatch.setenv("BACKFILL_SLOTS", "1")
    monkeypatch.setenv("BACKFILL_QUEUE_ARN", BACKFILL_ARN)
    monkeypatch.setenv("SLOT_WAIT_SECONDS", "0")
    dlq = boto3.client("sqs", region_name=REGION).create_queue(QueueName="backfill-dlq")
    monkeypatch.setenv("BACKFILL_DLQ_URL", dlq["QueueUrl"])
    aws.reset()
    monkeypatch.setattr(aws, "sfn", lambda: sfn)
    waiting: list[str] = []
    monkeypatch.setattr(dispatcher, "_wait_on_queue", lambda r: waiting.append(r.message_id))
    bodies = [
        json.dumps(message_from(produce(f"art_{i}", PAGE + " " * i), trigger="BACKFILL"))
        for i in range(2)
    ]
    event = {
        "Records": [
            sqs_record(bodies[0], BACKFILL_ARN, "m0"),
            sqs_record(bodies[1], BACKFILL_ARN, "m1"),
            sqs_record("{not json", BACKFILL_ARN, "m2"),
        ]
    }

    out = dispatcher.handler(event, InvocationContext())

    # One slot: m0 starts, m1 waits on the queue (no function error), m2 can never start.
    assert out["batchItemFailures"] == [{"itemIdentifier": "m1"}]
    assert waiting == ["m1"]
    assert [i["ctx"]["slot"] for i in sfn.inputs] == ["backfill#0"]
    dead = boto3.client("sqs", region_name=REGION).receive_message(QueueUrl=dlq["QueueUrl"])
    assert [m["Body"] for m in dead["Messages"]] == ["{not json"]


def test_every_way_a_run_ends_releases_its_slot(env: Env) -> None:
    store = aws.status_store()

    def run_with_slot(message: dict[str, Any], **kwargs: Any) -> tuple[str, str]:
        run_id = new_run_id()
        slot = store.acquire_slot("live", 4, run_id)
        assert slot is not None
        run(message, run_id=run_id, slot=slot, **kwargs)
        return run_id, slot

    record = produce("art_1", PAGE)
    _, indexed = run_with_slot(message_from(record))  # Finalize
    _, skipped = run_with_slot(message_from(record))  # Claim: unchanged, SKIP
    _, failed = run_with_slot(  # MarkFailed: permanent
        message_from(produce("art_2", PAGE + " ")), fail_at=StepName.EMBED
    )
    run_id, recorded = run_with_slot(message_from(produce("art_3", PAGE + "  ")))

    assert latest("art_1")["status"] == ArticleStatus.INDEXED
    assert latest("art_2")["status"] == ArticleStatus.FAILED_PERMANENT
    assert [slot_holder(s) for s in (indexed, skipped, failed, recorded)] == [None] * 4
    assert (store.get(keys.run_key("art_3", run_id)) or {})["slot"] == recorded  # kept on RUN#


def test_a_stale_run_releases_its_slot(env: Env) -> None:
    from ingest.steps import claim, fetch

    store = aws.status_store()
    message = message_from(produce("art_1", PAGE))
    winner = claim.handler({"ctx": {"message": message, "run_id": new_run_id()}}, FakeContext())
    loser_id = new_run_id()
    slot = store.acquire_slot("live", 2, loser_id)
    loser = claim.handler(
        {"ctx": {"message": message, "run_id": loser_id, "slot": slot}}, FakeContext()
    )
    assert loser["action"] == "STALE"
    assert slot_holder(str(slot)) is None

    # A run that loses the article mid-way ends in the Stale state, which runs no code.
    slot = store.acquire_slot("live", 2, winner["run_id"])
    store.table.update_item(
        Key=keys.latest_key("art_1"),
        UpdateExpression="SET lease_owner = :o, run_id = :o",
        ExpressionAttributeValues={":o": "another_run"},
    )
    with pytest.raises(IngestStale):
        fetch.handler({"ctx": winner | {"slot": slot}}, FakeContext())
    assert slot_holder(str(slot)) is None


# -- failure budgets and limits ----------------------------------------------------------------


def mark(ctx: dict[str, Any], error: str, code: str = "E_THROTTLED") -> dict[str, Any]:
    from ingest.steps import mark_failed

    cause = json.dumps({"errorMessage": json.dumps({"code": code, "detail": "busy"})})
    return dict(
        mark_failed.handler({"ctx": ctx, "error": {"Error": error, "Cause": cause}}, FakeContext())
    )


def claimed(article_id: str = "art_1") -> dict[str, Any]:
    from ingest.steps import claim

    message = message_from(produce(article_id, PAGE))
    return dict(claim.handler({"ctx": {"message": message, "run_id": new_run_id()}}, FakeContext()))


def test_throttling_has_its_own_retry_budget(env: Env) -> None:

    out = mark(claimed(), "Lambda.TooManyRequestsException", code="E_BUG")
    item = latest("art_1")

    assert out == {"outcome": "FAILED_RETRYABLE", "error_code": ErrorCode.THROTTLED.value}
    assert int(item["throttle_count"]) == 1
    assert "retry_count" not in item

    aws.status_store().table.update_item(
        Key=keys.latest_key("art_1"),
        UpdateExpression="SET throttle_count = :n",
        ExpressionAttributeValues={":n": get_settings().retry.max_throttled - 1},
    )
    again = claimed()  # the reconciler's retry: a new run of the article
    assert mark(again, "IngestThrottled")["outcome"] == "FAILED_PERMANENT"
    assert "throttled runs" in latest("art_1")["error_msg"]


def test_permanent_failures_are_counted_not_mailed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted: list[tuple[str, dict[str, str]]] = []
    alerts: list[str] = []
    monkeypatch.setattr(telemetry, "emit", lambda n, v, u=None, **d: emitted.append((n, d)))
    monkeypatch.setattr(events, "alert", lambda subject, detail: alerts.append(subject))

    run(message_from(produce("art_1", PAGE)), fail_at=StepName.EXTRACT)

    assert ("ArticlesFailedPermanent", {"error_code": "E_UNREADABLE"}) in emitted
    assert alerts == []  # the reconciler's digest reports it


def test_too_many_chunks_is_permanent(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.test_pipeline import handlers

    monkeypatch.setenv("MAX_CHUNKS_PER_ARTICLE", "1")
    aws.reset()
    ctx = claimed()
    for step in (StepName.FETCH, StepName.EXTRACT, StepName.NORMALIZE):
        handlers()[step]({"ctx": ctx}, FakeContext())

    with pytest.raises(IngestPermanent) as err:
        handlers()[StepName.CHUNK]({"ctx": ctx}, FakeContext())
    assert err.value.code == ErrorCode.TOO_LARGE
    assert "max_chunks_per_article (1)" in err.value.detail


# -- reconciler: digest and backlog -----------------------------------------------------------


def reconcile(now: int | None = None) -> dict[str, int]:
    from ingest.reconciler.app import Reconciler

    return Reconciler(aws.status_store(), now or now_epoch(), 500).run()


def test_one_digest_per_run_for_new_permanent_failures(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    alerts: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(events, "alert", lambda s, d: alerts.append((s, d)))
    run(message_from(produce("art_1", PAGE)), fail_at=StepName.EXTRACT)
    run(message_from(produce("art_2", PAGE + " ")), fail_at=StepName.EMBED)
    store = aws.status_store()  # and one the reconciler escalates in the same run
    run_id = claimed("art_3")["run_id"]
    store.mark_retryable(StepRef("art_3", run_id, "prc_A"), "E_TIMEOUT", "slow", now_epoch() - 1)
    store.table.update_item(
        Key=keys.latest_key("art_3"),
        UpdateExpression="SET retry_count = :n",
        ExpressionAttributeValues={":n": 5},
    )

    counts = reconcile(now_epoch() + 1)

    assert counts["failed_digest"] == 3
    [(subject, detail)] = alerts
    assert subject == "Ingestion: 3 article(s) failed permanently"
    assert detail["by_error_code"] == {"E_UNREADABLE": 2, "E_TIMEOUT": 1}
    assert sorted(detail["sample_article_ids"]) == ["art_1", "art_2", "art_3"]

    reconcile(now_epoch() + 2)
    assert len(alerts) == 1  # nothing new since the last digest


def test_backlog_metrics_per_status_and_overall(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[tuple[str, float, dict[str, str]]] = []
    monkeypatch.setattr(telemetry, "emit", lambda n, v, u=None, **d: emitted.append((n, v, d)))
    claimed("art_1")
    claimed("art_2")
    aws.status_store().table.update_item(  # art_2 has been STAGED for an hour
        Key=keys.latest_key("art_2"),
        UpdateExpression="SET #s = :st, gsi1pk = :st, gsi1sk = :old",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":st": "STAGED", ":old": iso_at(now_epoch() - 3600)},
    )

    reconcile()

    def value(name: str, status: str) -> float:
        return next(v for n, v, d in emitted if n == name and d == {"status": status})

    assert value("BacklogCount", "CLAIMED") == 1
    assert value("BacklogCount", "STAGED") == 1
    assert value("BacklogCount", "RUNNING") == 2
    assert value("BacklogCount", "FAILED_RETRYABLE") == 0
    assert 3590 <= value("BacklogOldestSeconds", "RUNNING") <= 3700
