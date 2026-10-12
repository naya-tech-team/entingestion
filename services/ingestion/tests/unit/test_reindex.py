"""Model change and rebuild (brief §7.3, UC9, UC10): per-table targets, the reindex plan and
batch handlers, the control API routes, and the tables function's view switch."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import boto3
import pytest

from ingest import aws
from ingest.models.status import StepName
from ingest.reindex import ReindexFilter, plan_items
from tests.conftest import REGION, STAGING_BUCKET
from tests.unit.test_operations import api, queued
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
NEW = "chunks_titan2_512"


def indexed(article_id: str = "art_1", body: str = PAGE, **kw: Any) -> dict[str, Any]:
    record = produce(article_id, body, **kw)
    run(message_from(record))
    return record


def reindex_message(record: dict[str, Any], stage: StepName, table: str | None) -> dict[str, Any]:
    return message_from(
        record, trigger="BACKFILL", from_stage=stage.value, target_table=table,
        requested_by="test",
    )  # fmt: skip


def staged_keys(article_id: str) -> set[str]:
    resp = boto3.client("s3", region_name=REGION).list_objects_v2(
        Bucket=STAGING_BUCKET, Prefix=f"{article_id}/"
    )
    return {o["Key"].split("/", 2)[2] for o in resp.get("Contents", [])}


# -- the pipeline with targets ---------------------------------------------------------------


def test_model_change_reembeds_into_the_new_table_only(env: Env) -> None:
    vectors, titan = env
    record = indexed()
    live_rows = dict(vectors.rows)
    calls = titan.calls

    _, outcome = run(reindex_message(record, StepName.EMBED, NEW))

    assert outcome["action"] == "DONE"
    new_table = vectors.tables[NEW]  # type: ignore[attr-defined]
    assert new_table.active("art_1")
    assert vectors.rows == live_rows  # the live table is untouched
    assert titan.calls > calls  # new model/dimension: no cache hits from the 1024-d vectors
    keys = staged_keys("art_1")
    assert {"embeddings/vectors.parquet", f"embeddings/{NEW}.parquet"} <= keys
    assert {"_manifest.json", f"_manifest.{NEW}.json"} <= keys
    assert latest("art_1")["status"] == "INDEXED"
    assert summary("prc_A") == {"indexed": 1, "skipped": 0, "failed": 0}  # nothing recounted


def test_rebuild_from_index_makes_no_bedrock_calls(env: Env) -> None:
    vectors, titan = env
    record = indexed()
    count = len(vectors.active("art_1"))
    vectors.rows.clear()  # "truncate"
    calls = titan.calls

    _, outcome = run(reindex_message(record, StepName.INDEX, None))

    assert outcome["action"] == "DONE"
    assert len(vectors.active("art_1")) == count
    assert titan.calls == calls


def test_rebuilding_the_old_table_after_a_model_change_still_works(env: Env) -> None:
    vectors, _ = env
    record = indexed()
    run(reindex_message(record, StepName.EMBED, NEW))
    vectors.rows.clear()

    _, outcome = run(reindex_message(record, StepName.INDEX, "chunks_titan2_1024"))

    assert outcome["action"] == "DONE"  # its own staged vectors and manifest are intact
    assert vectors.active("art_1")


def test_delete_removes_the_article_from_every_table(env: Env) -> None:
    vectors, _ = env
    record = indexed()
    run(reindex_message(record, StepName.EMBED, NEW))
    run(message_from(record) | {"event": "REMOVE", "s3_uri": None, "from_stage": None})

    assert not vectors.active("art_1")
    assert not vectors.tables[NEW].active("art_1")  # type: ignore[attr-defined]


# -- write tables, shadow runs ----------------------------------------------------------------


def _shadow(article_id: str, table: str) -> dict[str, Any]:
    from ingest.status import keys

    item = aws.status_store().get(keys.shadow_key(article_id, table))
    assert item is not None
    return item


def test_edits_during_a_model_change_reach_both_tables(env: Env) -> None:
    from ingest.vectorstore import registry

    vectors, _ = env
    indexed()
    registry.add_write(aws.status_store(), NEW, "test")  # the reindex started

    _, out = run(
        message_from(produce("art_1", PAGE + " Edited.", updated_at="2026-10-12T00:00:00.000Z"))
    )

    assert out["action"] == "DONE"
    assert {r["doc_version"] for r in vectors.active("art_1")} == {2}
    assert {r["doc_version"] for r in vectors.tables[NEW].active("art_1")} == {2}  # type: ignore[attr-defined]
    item = latest("art_1")
    assert (item["tv_chunks_titan2_1024"], item[f"tv_{NEW}"]) == (2, 2)


def test_a_new_table_failing_does_not_fail_the_live_run(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingest.vectorstore import registry

    vectors, titan = env
    registry.add_write(aws.status_store(), NEW, "test")
    real = titan.embed

    def broken_for_512(text: str) -> Any:
        if titan.dimension == 512:
            raise RuntimeError("model not enabled")
        return real(text)

    monkeypatch.setattr(titan, "embed", broken_for_512)

    _, out = run(message_from(produce("art_1", PAGE)))

    assert out["action"] == "DONE"
    assert latest("art_1")["status"] == "INDEXED"
    assert vectors.active("art_1")
    assert not vectors.tables.get(NEW, FakeVectorStore()).active("art_1")  # type: ignore[attr-defined]
    assert f"tv_{NEW}" not in latest("art_1")  # the drift check will fill it


def test_a_failed_shadow_run_leaves_the_live_status_alone(env: Env) -> None:
    record = indexed()

    _, out = run(reindex_message(record, StepName.EMBED, NEW), fail_at=StepName.EMBED)

    assert out["action"] == "FAILED"
    assert latest("art_1")["status"] == "INDEXED"
    assert summary("prc_A") == {"indexed": 1, "skipped": 0, "failed": 0}
    assert _shadow("art_1", NEW)["status"] == "FAILED_PERMANENT"


def test_a_shadow_run_for_superseded_content_does_nothing(env: Env) -> None:
    record = indexed()
    run(message_from(produce("art_1", PAGE + " v2", updated_at="2026-10-12T00:00:00.000Z")))

    _, out = run(reindex_message(record, StepName.EMBED, NEW))  # planned against v1's content

    assert out["action"] == "SKIP"


def test_a_shadow_run_does_not_hold_up_the_live_run(env: Env) -> None:
    from ingest.steps import claim

    record = indexed()
    shadow_ctx = claim.handler(
        {"ctx": {"message": reindex_message(record, StepName.EMBED, NEW), "run_id": "shadow1"}},
        FakeContext(),
    )
    assert shadow_ctx["lane"] == NEW

    changed = produce("art_1", PAGE + " v2", updated_at="2026-10-12T00:00:00.000Z")
    _, out = run(message_from(changed))  # the live run claims LATEST while the shadow runs

    assert out["action"] == "DONE"
    assert latest("art_1")["doc_version"] == 2
    from ingest.errors import IngestStale
    from tests.unit.test_pipeline import handlers

    with pytest.raises(IngestStale):  # the shadow run's version is gone: it stops
        handlers()[StepName.EMBED]({"ctx": shadow_ctx}, FakeContext())


def test_after_the_switch_runs_write_the_new_live_table_first(env: Env) -> None:
    from ingest.vectorstore import registry

    vectors, _ = env
    store = aws.status_store()
    registry.add_write(store, NEW, "test")
    registry.set_live(store, NEW, "test")

    _, out = run(message_from(produce("art_9", PAGE)))

    assert out["action"] == "DONE"
    assert vectors.tables[NEW].active("art_9")  # type: ignore[attr-defined]
    assert vectors.active("art_9")  # the old table is still written: rollback is exact
    manifest = boto3.client("s3", region_name=REGION).get_object(
        Bucket=STAGING_BUCKET, Key=f"art_9/v1/_manifest.{NEW}.json"
    )
    assert json.loads(manifest["Body"].read())["dimension"] == 512


# -- plan and enqueue -----------------------------------------------------------------------


def test_plan_filters(env: Env) -> None:
    indexed("art_1")
    indexed("art_2", PAGE + " ", process_id="prc_B")
    store = aws.status_store()

    assert plan_items(store, ReindexFilter()) == ["art_1", "art_2"]
    assert plan_items(store, ReindexFilter(process_id="prc_B")) == ["art_2"]
    assert plan_items(store, ReindexFilter(pipeline_version="v0")) == []
    assert plan_items(store, ReindexFilter(since="2999-01-01")) == []


def test_enqueue_batch_sends_backfill_messages(env: Env) -> None:
    from ingest.reindex import enqueue

    indexed("art_1")
    out = enqueue(
        {
            "Items": [{"article_id": "art_1"}, {"article_id": "art_gone"}],
            "BatchInput": {"target_table": NEW, "from_stage": "EMBED", "requested_by": "op"},
        },
        FakeContext(),  # type: ignore[arg-type]
    )

    assert out == {"queued": 1, "missing": ["art_gone"]}
    url = aws.get_settings().backfill_queue_url
    msg = json.loads(
        boto3.client("sqs", region_name=REGION).receive_message(QueueUrl=url)["Messages"][0]["Body"]
    )
    assert (msg["trigger"], msg["from_stage"], msg["target_table"]) == ("BACKFILL", "EMBED", NEW)
    assert queued() == []  # not on the live ingest queue


# -- control API -----------------------------------------------------------------------------


class FakeSfn:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []

    def start_execution(self, **kwargs: Any) -> dict[str, str]:
        self.started.append(kwargs)
        return {
            "executionArn": f"arn:aws:states:us-west-2:123456789012:execution:r:{kwargs['name']}"
        }


def test_reindex_route(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    sfn = FakeSfn()
    monkeypatch.setattr(aws, "sfn", lambda: sfn)
    indexed()

    status, out = api("POST", "/v1/reindex", {"target_table": NEW, "dry_run": True})
    assert (status, out["count"], sfn.started) == (200, 1, [])

    status, out = api("POST", "/v1/reindex", {"target_table": NEW, "from_stage": "EMBED"})
    assert status == 202
    request = json.loads(sfn.started[0]["input"])
    assert (request["target_table"], request["from_stage"]) == (NEW, "EMBED")
    assert request["requested_by"].startswith("arn:aws:iam::")

    status, out = api("POST", "/v1/reindex", {"from_stage": "INDEX"})
    assert status == 202
    assert json.loads(sfn.started[1]["input"])["target_table"] == "chunks_titan2_1024"


@pytest.mark.parametrize(
    "body", [{"target_table": "nope"}, {"from_stage": "FETCH"}, {"filter": {"x": 1}}]
)
def test_reindex_rejects_bad_requests(env: Env, body: dict[str, Any]) -> None:
    assert api("POST", "/v1/reindex", body)[0] == 422


class _TablesFake:
    calls: ClassVar[list[dict[str, Any]]] = []

    def invoke(self, FunctionName: str, Payload: bytes) -> dict[str, Any]:
        event = json.loads(Payload)
        self.calls.append(event | {"function": FunctionName})
        out = {"active_table": event.get("table", "chunks_titan2_1024")}
        return {"Payload": _Body(json.dumps(out).encode())}


def test_the_switch_is_gated_on_writes_and_coverage(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingest.vectorstore import registry

    _TablesFake.calls = []
    monkeypatch.setattr(aws, "lambda_client", _TablesFake)
    record = indexed()

    status, body = api("GET", "/v1/tables")
    assert (body["registry"]["live"], body["consistent"]) == ("chunks_titan2_1024", True)
    assert api("POST", "/v1/tables/active", {"table": "nope"})[0] == 422

    # Not written by live runs yet: refused.
    assert api("POST", "/v1/tables/active", {"table": NEW})[0] == 409

    registry.add_write(aws.status_store(), NEW, "test")
    status, body = api("POST", "/v1/tables/active", {"table": NEW})
    assert (status, body["missing"], body["sample"]) == (409, 1, ["art_1"])  # not filled yet

    run(reindex_message(record, StepName.EMBED, NEW))  # fill it
    status, body = api("POST", "/v1/tables/active", {"table": NEW})
    assert (status, body["gate"]) == (409, "evaluation")  # filled, but never evaluated
    put_report("chunks_titan2_1024", 0.80)
    put_report(NEW, 0.79)  # within the 0.02 tolerance
    status, body = api("POST", "/v1/tables/active", {"table": NEW})
    assert (status, body["active_table"], body["tables"]["live"]) == (200, NEW, NEW)
    assert body["tables"]["writes"] == [NEW, "chunks_titan2_1024"]  # old table kept: exact rollback
    assert [c["action"] for c in _TablesFake.calls] == ["status", "activate"]
    # Run as ingest_ops by the tables function, never by migrate (master credentials).
    assert {c["function"] for c in _TablesFake.calls} == {"ingestion-test-tables"}

    # The live table can't be retired; the old one can, after the switch.
    assert api("DELETE", f"/v1/tables/writes/{NEW}")[0] == 409
    status, body = api("DELETE", "/v1/tables/writes/chunks_titan2_1024")
    assert (status, body["writes"]) == (200, [NEW])


def put_report(
    table: str, recall: float, *, golden: str = "_eval/golden/20261001-0000.jsonl", days: int = 0
) -> None:
    """An evaluation report as the eval function writes it."""
    at = datetime.now(UTC) - timedelta(days=days)
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=STAGING_BUCKET,
        Key=f"_eval/{table}/{at.strftime('%Y%m%dT%H%M%SZ')}.json",
        Body=json.dumps(
            {"table": table, "at": at.isoformat(timespec="seconds").replace("+00:00", "Z"),
             "golden": golden, "recall_at_10": recall}
        ).encode(),
    )  # fmt: skip


@pytest.mark.parametrize(
    ("reports", "allowed", "message"),
    [
        ([("live", 0.80, {}), ("new", 0.70, {})], False, "more than 0.02 below"),
        ([("live", 0.80, {}), ("new", 0.85, {"days": 8})], False, "8 days old"),
        ([("live", 0.80, {}), ("new", 0.85, {"golden": "_eval/golden/other.jsonl"})], False,
         "different golden sets"),
        ([("new", 0.85, {})], False, "chunks_titan2_1024 has no evaluation"),
        ([("live", 0.80, {"days": 30}), ("new", 0.785, {})], True, ""),  # live may be older
    ],
)  # fmt: skip
def test_the_switch_is_gated_on_evaluation(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    reports: list[tuple[str, float, dict[str, Any]]],
    allowed: bool,
    message: str,
) -> None:
    from ingest.vectorstore import registry

    monkeypatch.setattr(aws, "lambda_client", _TablesFake)
    registry.add_write(aws.status_store(), NEW, "test")  # no indexed articles: coverage is fine
    for which, recall, kw in reports:
        put_report(NEW if which == "new" else "chunks_titan2_1024", recall, **kw)

    status, body = api("POST", "/v1/tables/active", {"table": NEW})

    assert status == (200 if allowed else 409)
    if not allowed:
        assert message in body["message"]
        assert body["gate"] == "evaluation"


def test_force_switches_despite_gaps(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from ingest.vectorstore import registry

    monkeypatch.setattr(aws, "lambda_client", _TablesFake)
    indexed()
    registry.add_write(aws.status_store(), NEW, "test")

    status, body = api("POST", "/v1/tables/active", {"table": NEW, "force": True})

    assert (status, body["tables"]["live"]) == (200, NEW)


class _Body:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> bytes:
        return self.data


# -- tables: which table the view serves -----------------------------------------------------


def test_active_table_is_read_from_the_view_definition() -> None:
    from ingest.migrate import active_table

    class Conn:
        def execute(self, *_: Any) -> Any:
            return self

        def fetchone(self) -> tuple[str]:
            return (
                " SELECT chunks_titan2_512.chunk_id,\n    chunks_titan2_512.article_id\n"
                "   FROM chunks_titan2_512\n  WHERE chunks_titan2_512.is_active;",
            )

    assert active_table(Conn()) == NEW  # type: ignore[arg-type]
