"""Retrieval evaluation scoring and the reconciler's weekly chunk-count drift check."""

from __future__ import annotations

import json
import math
from typing import Any

import boto3
import pytest

from ingest import aws
from ingest.timeutil import now_epoch
from tests.conftest import REGION, STAGING_BUCKET
from tests.unit.test_operations import queued
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


class _Body:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> bytes:
        return self.data


class FakeLambda:
    """Stands in for the query function inside the VPC."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def invoke(self, FunctionName: str, Payload: bytes) -> dict[str, Any]:
        event = json.loads(Payload)
        self.calls.append(event)
        out = self.responses[event["action"]]
        return {"Payload": _Body(json.dumps(out(event) if callable(out) else out).encode())}


# -- scoring ---------------------------------------------------------------------------------


def test_articles_rank_by_their_best_chunk() -> None:
    from ingest.evaluation import article_ranking

    hits = [{"article_id": a} for a in ("b", "a", "b", "c", "a")]
    assert article_ranking(hits) == ["b", "a", "c"]


def test_chunk_level_recall_ndcg_and_mrr() -> None:
    from ingest.evaluation import judge

    ranked = [("x", "A"), ("r1", "B"), ("y", "C"), ("r2", "B")]
    ranked += [(f"z{i}", "D") for i in range(10)]
    hits = [{"chunk_id": c, "article_id": a} for c, a in ranked]
    j = judge(hits, {"r1", "r2", "r3"}, "B")

    assert j.recall_at_5 == pytest.approx(2 / 3)  # ranks 2 and 4
    assert j.recall_at_10 == pytest.approx(2 / 3)
    assert j.mrr == pytest.approx(1 / 2)
    ideal = 1 + 1 / math.log2(3) + 1 / math.log2(4)
    assert j.ndcg_at_10 == pytest.approx((1 / math.log2(3) + 1 / math.log2(5)) / ideal)
    assert j.article_recall_at_10 == 1.0
    with pytest.raises(ValueError, match="no relevant chunk_ids"):
        judge(hits, set(), "C")


def test_a_golden_set_needs_its_pointer_and_chunk_labels() -> None:
    from ingest.evaluation import GOLDEN_POINTER, load_golden

    with pytest.raises(FileNotFoundError):
        load_golden(lambda key: None)
    files = {GOLDEN_POINTER: b"_eval/golden/a.jsonl", "_eval/golden/a.jsonl": b'{"id": "1"}'}
    with pytest.raises(ValueError, match="no chunk_ids"):
        load_golden(files.get)


def test_bootstrap_interval_brackets_the_mean() -> None:
    from ingest.evaluation import Judged, bootstrap_ci, summarize

    values = [1.0] * 30 + [0.0] * 10
    low, high = bootstrap_ci(values)
    assert low < 0.75 < high
    assert bootstrap_ci(values) == (low, high)  # seeded: reports are reproducible
    out = summarize([Judged(1, 1, 1, 1, 1), Judged(0, 0, 0, 0, 0)])
    assert (out["questions"], out["recall_at_10"]) == (2, 0.5)
    assert out["recall_at_10_ci95"][0] <= 0.5 <= out["recall_at_10_ci95"][1]


def test_eval_handler_scores_the_golden_set(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from ingest import evaluation

    run(message_from(produce("art_1", PAGE)))
    run(message_from(produce("art_2", PAGE)))
    run(message_from(produce("art_3", PAGE)))
    run(message_from(produce("art_3", PAGE, updated_at="2026-10-11T01:00:00.000Z", title="New")))
    from ingest.vectorstore import index as vector_index

    vector_index.get_vector_store().delete_article("art_2", 1)
    aws.status_store().table.update_item(
        Key={"pk": "art_2", "sk": "LATEST"},
        UpdateExpression="SET #s = :d",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":d": "DELETED"},
    )
    golden = [
        {"id": "1", "question": "q1", "article_id": "art_1", "doc_version": 1, "chunk_ids": ["c1"]},
        {"id": "2", "question": "q2", "article_id": "art_2", "doc_version": 1, "chunk_ids": ["c2"]},
        {"id": "3", "question": "q3", "article_id": "art_3", "doc_version": 1, "chunk_ids": ["c3"]},
        {"id": "4", "question": "nothing", "no_answer": True},
    ]
    s3 = boto3.client("s3", region_name=REGION)
    key = "_eval/golden/20261011-0000.jsonl"
    s3.put_object(Bucket=STAGING_BUCKET, Key=key,
                  Body="\n".join(json.dumps(g) for g in golden).encode())  # fmt: skip
    s3.put_object(Bucket=STAGING_BUCKET, Key=evaluation.GOLDEN_POINTER, Body=key.encode())
    fake = FakeLambda(
        {
            "describe": {"active_table": "chunks_primary", "dimension": 1024},
            "search": lambda e: {
                "results": [
                    [{"chunk_id": "x", "article_id": "art_9", "score": 0.9},
                     {"chunk_id": "c1", "article_id": "art_1", "score": 0.8}],
                    [{"chunk_id": "x", "article_id": "art_9", "score": 0.3}],
                ][: len(e["vectors"])]
            },
        }
    )  # fmt: skip
    monkeypatch.setattr(aws, "lambda_client", lambda: fake)
    monkeypatch.setattr(evaluation, "make_embedder", lambda profile: FakeTitan())

    out = evaluation.handler({"table": None}, FakeContext())  # type: ignore[arg-type]

    assert (out["table"], out["live"], out["questions"]) == ("chunks_primary", True, 1)
    assert out["dropped"] == {"deleted": 1, "stale": 1}  # art_2 deleted; art_3 is at v2 now
    assert out["recall_at_10"] == 1.0
    assert out["mrr"] == 0.5
    assert out["golden"] == key
    assert out["no_answer"]["mean_top_score"] == pytest.approx(0.3)
    report_key = out["report"].split("/", 3)[3]
    assert report_key.startswith("_eval/chunks_primary/")
    assert evaluation.latest_report("chunks_primary")["key"] == report_key  # type: ignore[index]


def test_a_baseline_run_also_scores_the_live_table(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingest import evaluation

    s3 = boto3.client("s3", region_name=REGION)
    key = "_eval/golden/20261011-0000.jsonl"
    line = {"id": "1", "question": "q", "article_id": "art_1", "doc_version": 1, "chunk_ids": ["c"]}
    s3.put_object(Bucket=STAGING_BUCKET, Key=key, Body=json.dumps(line).encode())
    s3.put_object(Bucket=STAGING_BUCKET, Key=evaluation.GOLDEN_POINTER, Body=key.encode())
    fake = FakeLambda(
        {
            "describe": {"active_table": "chunks_primary", "dimension": 1024},
            "search": lambda e: {
                "results": [[{"chunk_id": "c" if e["table"] else "d", "article_id": "art_1",
                              "score": 0.5}]]
            },
        }
    )  # fmt: skip
    monkeypatch.setattr(aws, "lambda_client", lambda: fake)
    monkeypatch.setattr(evaluation, "current", lambda qs: (qs, {"deleted": 0, "stale": 0}))
    monkeypatch.setattr(evaluation, "make_embedder", lambda profile: FakeTitan())

    out = evaluation.handler(  # type: ignore[arg-type]
        {"table": "chunks_compact", "baseline": True}, FakeContext()
    )

    assert out["recall_at_10"] == 1.0
    assert out["baseline"]["table"] == "chunks_primary"
    assert out["baseline"]["recall_at_10"] == 0.0
    assert out["delta"]["recall_at_10"] == 1.0


# -- weekly drift check ----------------------------------------------------------------------


def backfill_queued() -> list[dict[str, Any]]:
    sqs = boto3.client("sqs", region_name=REGION)
    url = aws.get_settings().queues.backfill_url
    out: list[dict[str, Any]] = []
    while messages := sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10).get("Messages"):
        out += [json.loads(m["Body"]) for m in messages]
        for m in messages:
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
    return out


def test_drift_rebuilds_mismatches_and_removes_orphans(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingest.reconciler.app import Reconciler

    run(message_from(produce("art_1", PAGE)))
    run(message_from(produce("art_2", PAGE + " ")))
    count = int(latest("art_1")["chunk_count"])
    fake = FakeLambda(
        # art_1 lost a row, art_2 is fine, art_gone has rows but no LATEST
        {
            "versions": {
                "rows": [["art_1", 1, count - 1], ["art_2", 1, count], ["art_gone", 1, 3]],
                "next": None,
            }
        }
    )
    monkeypatch.setattr(aws, "lambda_client", lambda: fake)

    counts = Reconciler(aws.status_store(), now_epoch(), 500).run(drift=True)

    assert counts["requeued_drift_repair"] == 1
    repairs = {m["article_id"]: m for m in backfill_queued()}
    # A shadow run rebuilds the live table from staging: it never touches the live status.
    assert set(repairs) == {"art_1"}
    assert (repairs["art_1"]["from_stage"], repairs["art_1"]["target_table"]) == (
        "INDEX",
        "chunks_primary",
    )
    messages = {m["article_id"]: m for m in queued()}
    # Rows with no LATEST: the delete path runs anyway (force) so they don't linger.
    assert (messages["art_gone"]["event"], messages["art_gone"]["force"]) == ("REMOVE", True)
    assert counts["requeued_drift_orphan"] == 1


def test_drift_writes_indexed_articles_missing_from_a_new_table(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingest.reconciler.app import Reconciler
    from ingest.vectorstore import registry

    run(message_from(produce("art_1", PAGE)))
    registry.add_write(aws.status_store(), "chunks_compact", "test")  # a model change began
    monkeypatch.setattr(
        aws, "lambda_client", lambda: FakeLambda({"versions": {"rows": [], "next": None}})
    )

    Reconciler(aws.status_store(), now_epoch(), 500).run(drift=True)

    repairs = backfill_queued()
    assert [(m["article_id"], m["target_table"], m["from_stage"]) for m in repairs] == [
        ("art_1", "chunks_compact", "EMBED")
    ]


def test_hourly_runs_skip_the_drift_check(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from ingest.reconciler.app import Reconciler

    fake = FakeLambda({})
    monkeypatch.setattr(aws, "lambda_client", lambda: fake)
    Reconciler(aws.status_store(), now_epoch(), 500).run()
    assert fake.calls == []
