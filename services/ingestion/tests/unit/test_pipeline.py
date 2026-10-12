"""The pipeline end to end, in process: the real handlers run in state-machine order against moto,
with Bedrock stubbed and pgvector replaced by an in-memory store. Asserts the record counts of
brief §6.3 and the UC3/UC5/DUP/CONC/delete scenarios."""

from __future__ import annotations

import hashlib
import json
import math
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import boto3
import pytest

from ingest import aws
from ingest.embedding.base import Embedding
from ingest.errors import IngestError, IngestStale
from ingest.ids import new_run_id
from ingest.models.status import StepName
from ingest.status import keys
from ingest.vectorstore.index import COLUMNS, IndexResult, IndexRow
from tests.conftest import RAW_BUCKET, REGION

PAGE = (
    "<html><head><title>Article</title></head><body><article><h1>Article</h1>"
    + "".join(
        f"<h2>Part {i}</h2><p>{f'Content sentence number {i} is here. ' * 30}</p>" for i in range(3)
    )
    + "</article></body></html>"
)


class FakeContext:
    function_name = "test"
    memory_limit_in_mb = 512
    invoked_function_arn = "arn:aws:lambda:us-west-2:123456789012:function:test"
    aws_request_id = "req"


# One tombstone table per database, shared by every vector table (as in Postgres).
TOMBSTONES: dict[str, int] = {}


@dataclass
class FakeVectorStore:
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    metadata_updates: int = 0

    def write_and_swap(
        self, article_id: str, doc_version: int, rows: list[IndexRow]
    ) -> IndexResult:
        if any(
            r["article_id"] == article_id and r["doc_version"] > doc_version
            for r in self.rows.values()
        ):
            raise IngestStale("newer version indexed")
        if TOMBSTONES.get(article_id, -1) >= doc_version:
            raise IngestStale("deleted")
        if TOMBSTONES.get(article_id, doc_version) < doc_version:
            del TOMBSTONES[article_id]
        for row in rows:
            values = dict(zip(COLUMNS, row.values(), strict=True))
            self.rows[row.chunk.chunk_id] = {
                **{k: values[k] for k in ("tenant_id", "source", "published_at", "text")},
                "article_id": article_id,
                "doc_version": doc_version,
                "active": False,
                "metadata": json.loads(values["metadata"]),
            }
        old = [
            cid
            for cid, r in self.rows.items()
            if r["article_id"] == article_id and r["doc_version"] < doc_version
        ]
        for cid in old:
            del self.rows[cid]
        for r in self.rows.values():
            if r["article_id"] == article_id:
                r["active"] = r["doc_version"] == doc_version
        return IndexResult(written=len(rows), deleted_old=len(old))

    def update_metadata(self, article_id: str, attributes: Any) -> int:
        active = self.active(article_id)
        for r in active:
            r |= {
                "tenant_id": attributes.tenant_id,
                "source": attributes.source,
                "published_at": attributes.published_day,
                "metadata": attributes.apply(r["metadata"]),
            }
        self.metadata_updates += 1
        return len(active)

    def retag_tenant(self, article_id: str, tenant_id: str) -> int:
        stale = [
            r
            for r in self.rows.values()
            if r["article_id"] == article_id and r["tenant_id"] != tenant_id
        ]
        for r in stale:
            r["tenant_id"] = tenant_id
            r["metadata"] = {**r["metadata"], "tenant_id": tenant_id}
        return len(stale)

    def delete_article(self, article_id: str, doc_version: int = 0) -> int:
        TOMBSTONES[article_id] = max(TOMBSTONES.get(article_id, 0), doc_version)
        doomed = [cid for cid, r in self.rows.items() if r["article_id"] == article_id]
        for cid in doomed:
            del self.rows[cid]
        return len(doomed)

    def active(self, article_id: str) -> list[dict[str, Any]]:
        return [r for r in self.rows.values() if r["article_id"] == article_id and r["active"]]


@dataclass
class FakeTitan:
    """A deterministic unit-length embedder (the Embedder interface, one text per call)."""

    calls: int = 0
    dimension: int = 1024
    batch_size: int = 1

    def embed_documents(self, texts: Sequence[str]) -> list[Embedding]:
        return [self.embed(t) for t in texts]

    def embed_query(self, text: str) -> Embedding:
        return self.embed(text)

    def embed(self, text: str) -> Embedding:
        self.calls += 1
        seed = hashlib.sha256(text.encode()).digest()
        raw = [((seed[i % 32] + i) % 17) - 8.0 + 0.5 for i in range(self.dimension)]
        norm = math.sqrt(sum(x * x for x in raw))
        return Embedding([x / norm for x in raw], len(text.split()))


@pytest.fixture
def env(aws: None, monkeypatch: pytest.MonkeyPatch) -> tuple[FakeVectorStore, FakeTitan]:
    from ingest.steps import embed as embed_step
    from ingest.vectorstore import index as vector_index

    TOMBSTONES.clear()
    store, titan = FakeVectorStore(), FakeTitan()
    tables: dict[str, FakeVectorStore] = {"chunks_titan2_1024": store}
    monkeypatch.setattr(
        vector_index,
        "get_vector_store",
        lambda table=None: tables.setdefault(table or "chunks_titan2_1024", FakeVectorStore()),
    )
    store.tables = tables  # type: ignore[attr-defined]  # other tables, for reindex tests

    def embedder(ctx: Any, t: Any) -> FakeTitan:
        titan.dimension = t.dimension
        return titan

    monkeypatch.setattr(embed_step, "embedder", embedder)
    return store, titan


def produce(
    article_id: str,
    body: str,
    process_id: str = "prc_A",
    updated_at: str = "2026-10-11T00:00:00.000Z",
    **fields: Any,
) -> dict[str, Any]:
    """Write a producer-side raw object + articles row; return the stream record the Pipe sends.
    fields override the row's title, metadata (string pairs), tenant_id, ..."""
    data = body.encode()
    key = f"raw/{article_id}/upl_{abs(hash(body)) % 10**8:08d}/doc.html"
    put = boto3.client("s3", region_name=REGION).put_object(Bucket=RAW_BUCKET, Key=key, Body=data)
    sha = hashlib.sha256(data).hexdigest()
    row = {
        "article_id": article_id,
        "process_id": process_id,
        "source": "news",
        "title": "Article",
        "metadata": {"lang": "en"},
        "s3_uri": f"s3://{RAW_BUCKET}/{key}",
        "s3_version_id": put["VersionId"],
        "content_sha256": sha,
        "status": "STAGED",
        "updated_at": updated_at,
        "updated_date": updated_at[:10],
        "updated_shard": f"{updated_at[:10]}#{zlib.crc32(article_id.encode()) % 16:02d}",
        **fields,
    }
    boto3.resource("dynamodb", region_name=REGION).Table("articles").put_item(Item=row)
    image = {
        k: ({"M": {mk: {"S": mv} for mk, mv in v.items()}} if k == "metadata" else {"S": v})
        for k, v in row.items()
    }
    return {"eventName": "MODIFY", "dynamodb": {"NewImage": image}}


def message_from(record: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    from ingest.dispatcher import to_message

    return to_message(record).model_dump(mode="json") | overrides


ORDER = [
    StepName.FETCH,
    StepName.EXTRACT,
    StepName.NORMALIZE,
    StepName.CHUNK,
    StepName.EMBED,
    StepName.VALIDATE,
    StepName.INDEX,
    StepName.FINALIZE,
]


def handlers() -> dict[StepName, Any]:
    from ingest.steps import chunk, embed, extract, fetch, finalize, index, normalize, validate

    return {
        StepName.FETCH: fetch.handler,
        StepName.EXTRACT: extract.handler,
        StepName.NORMALIZE: normalize.handler,
        StepName.CHUNK: chunk.handler,
        StepName.EMBED: embed.handler,
        StepName.VALIDATE: validate.handler,
        StepName.INDEX: index.handler,
        StepName.FINALIZE: finalize.handler,
    }


def run(
    message: dict[str, Any],
    fail_at: StepName | None = None,
    run_id: str | None = None,
    slot: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Drive one execution the way the state machine does. Returns (run_id, final outcome info).
    slot: the admission slot the dispatcher would have put in the input."""
    from ingest.steps import claim, mark_failed

    run_id = run_id or new_run_id()
    start = {"message": message, "run_id": run_id} | ({"slot": slot} if slot else {})
    ctx = claim.handler({"ctx": start, "execution_arn": "arn:exec"}, FakeContext())
    if ctx["action"] in ("SKIP", "STALE", "DONE"):
        return run_id, {"action": ctx["action"]}
    steps = (
        [StepName.INDEX]
        if ctx["mode"] in ("DELETE", "METADATA")
        else ORDER[ORDER.index(StepName(ctx["start_at"])) :]
    )
    for name in steps:
        try:
            if name == fail_at:
                raise_in_step(name, {"ctx": ctx})
            handlers()[name]({"ctx": ctx}, FakeContext())
        except IngestStale:
            return run_id, {"action": "STALE"}
        except IngestError as err:
            cause = json.dumps({"errorType": type(err).__name__, "errorMessage": str(err)})
            out = mark_failed.handler(
                {"ctx": ctx, "error": {"Error": type(err).__name__, "Cause": cause}}, FakeContext()
            )
            return run_id, {"action": "FAILED", **out}
    return run_id, {"action": "DONE"}


def raise_in_step(name: StepName, event: dict[str, Any]) -> None:
    """Make step `name` fail permanently inside the real protocol (so its step item is written)."""
    from ingest.errors import ErrorCode, make_error
    from ingest.steps.protocol import StepContext, StepOutput, step

    @step(name)
    def failing(ctx: StepContext) -> StepOutput:
        raise make_error(ErrorCode.UNREADABLE, "corrupt file")

    failing(event, FakeContext())


def run_items(article_id: str, run_id: str) -> list[dict[str, Any]]:
    return aws.status_store().run_items(article_id, run_id)


def latest(article_id: str) -> dict[str, Any]:
    item = aws.status_store().latest(article_id)
    assert item is not None
    return item


def summary(process_id: str) -> dict[str, int]:
    s = aws.status_store().summary(process_id) or {}
    return {k: int(s.get(k, 0)) for k in ("indexed", "skipped", "failed")}


# -- UC1 + record counts --------------------------------------------------------------------


def test_new_article_is_indexed_with_ten_records(env: tuple[FakeVectorStore, FakeTitan]) -> None:
    store, _ = env
    run_id, out = run(message_from(produce("art_1", PAGE)))

    assert out["action"] == "DONE"
    item = latest("art_1")
    assert item["status"] == "INDEXED"
    assert item["chunk_count"] == len(store.active("art_1")) > 0
    assert "lease_owner" not in item
    # First run: RUN# + 8 step items + the new LATEST = 10 records (brief §6.3).
    assert len(run_items("art_1", run_id)) == 9
    assert all(
        i["step_status"] == "SUCCEEDED" for i in run_items("art_1", run_id) if "#STEP#" in i["sk"]
    )
    assert summary("prc_A") == {"indexed": 1, "skipped": 0, "failed": 0}

    staged = boto3.client("s3", region_name=REGION).list_objects_v2(
        Bucket="ingest-staging-test", Prefix="art_1/v1/"
    )
    names = sorted(o["Key"].removeprefix("art_1/v1/") for o in staged["Contents"])
    assert names == [
        "_manifest.json",
        "chunks/chunks.jsonl",
        "embeddings/vectors.parquet",
        "extracted/content.json",
        "normalized/document.json",
    ]


def test_changed_content_indexes_v2_with_nine_records_and_cache_hits(
    env: tuple[FakeVectorStore, FakeTitan],
) -> None:
    store, titan = env
    run(message_from(produce("art_1", PAGE)))
    first_calls = titan.calls

    changed = PAGE.replace(
        "</article>", "<h2>Extra</h2><p>A brand new closing section.</p></article>"
    )
    run_id, _ = run(message_from(produce("art_1", changed, updated_at="2026-10-11T01:00:00.000Z")))

    assert latest("art_1")["doc_version"] == 2
    assert len(run_items("art_1", run_id)) == 9  # clean run on an existing article
    assert {r["doc_version"] for r in store.active("art_1")} == {2}
    assert titan.calls - first_calls < first_calls  # unchanged chunks came from the cache (UC2)


# -- UC3: unchanged / PATCH ---------------------------------------------------------------------


def test_unchanged_reregistration_is_one_record_and_counted_as_skipped(
    env: tuple[FakeVectorStore, FakeTitan],
) -> None:
    _, titan = env
    run(message_from(produce("art_1", PAGE)))
    calls = titan.calls

    run_id, out = run(
        message_from(
            produce("art_1", PAGE, process_id="prc_B", updated_at="2026-10-11T02:00:00.000Z")
        )
    )

    assert out["action"] == "SKIP"
    assert len(run_items("art_1", run_id)) == 1
    assert titan.calls == calls  # no Bedrock
    assert summary("prc_B") == {"indexed": 0, "skipped": 1, "failed": 0}


# -- failures, UC5 ----------------------------------------------------------------------------


@pytest.mark.parametrize("fail_at", [StepName.FETCH, StepName.CHUNK, StepName.INDEX])
def test_failure_at_step_n_writes_n_records(
    env: tuple[FakeVectorStore, FakeTitan], fail_at: StepName
) -> None:
    run_id, out = run(message_from(produce("art_1", PAGE)), fail_at=fail_at)

    assert out["outcome"] == "FAILED_PERMANENT"
    assert len(run_items("art_1", run_id)) == fail_at.number  # RUN# + steps 2..N
    item = latest("art_1")
    assert (item["status"], item["error_code"]) == ("FAILED_PERMANENT", "E_UNREADABLE")
    assert summary("prc_A") == {"indexed": 0, "skipped": 0, "failed": 1}


def test_fix_and_replay_moves_failed_to_indexed(env: tuple[FakeVectorStore, FakeTitan]) -> None:
    record = produce("art_1", PAGE)
    run(message_from(record), fail_at=StepName.EXTRACT)

    run_id, out = run(message_from(record, force=True, trigger="MANUAL"))

    assert out["action"] == "DONE"
    assert latest("art_1")["status"] == "INDEXED"
    assert summary("prc_A") == {"indexed": 1, "skipped": 0, "failed": 0}  # failed -1, indexed +1
    run_item = next(i for i in run_items("art_1", run_id) if "#STEP#" not in i["sk"])
    assert run_item["force"] is True


def test_replay_from_a_step_only_reruns_the_rest(env: tuple[FakeVectorStore, FakeTitan]) -> None:
    record = produce("art_1", PAGE)
    run(message_from(record))

    run_id, _ = run(message_from(record, from_stage="INDEX", trigger="MANUAL"))

    steps = sorted(
        i["sk"].split("#STEP#")[1] for i in run_items("art_1", run_id) if "#STEP#" in i["sk"]
    )
    assert steps == ["08_INDEX", "09_FINALIZE"]  # replay: 1 + steps re-run
    assert latest("art_1")["doc_version"] == 1


def test_empty_document_is_skipped(env: tuple[FakeVectorStore, FakeTitan]) -> None:
    _, out = run(message_from(produce("art_1", "<html><body></body></html>")))

    assert out["outcome"] == "SKIPPED"
    assert latest("art_1")["skip_reason"] == "E_EMPTY_TEXT"
    assert summary("prc_A") == {"indexed": 0, "skipped": 1, "failed": 0}


def test_tampered_source_is_permanent(env: tuple[FakeVectorStore, FakeTitan]) -> None:
    record = produce("art_1", PAGE)
    message = message_from(record, content_sha256="0" * 64)

    _, out = run(message)

    assert (out["outcome"], out["error_code"]) == ("FAILED_PERMANENT", "E_SOURCE_CHANGED")


# -- delete, DUP, CONC ---------------------------------------------------------------------------


def test_delete_removes_rows_with_two_records(env: tuple[FakeVectorStore, FakeTitan]) -> None:
    store, _ = env
    run(message_from(produce("art_1", PAGE)))
    remove = {
        "eventName": "REMOVE",
        "dynamodb": {"OldImage": {"article_id": {"S": "art_1"}, "updated_at": {"S": "t"}}},
    }

    run_id, out = run(message_from(remove))

    assert out["action"] == "DONE"
    assert latest("art_1")["status"] == "DELETED"
    assert store.active("art_1") == []
    assert len(run_items("art_1", run_id)) == 2


def test_duplicate_deliveries_start_one_execution(
    env: tuple[FakeVectorStore, FakeTitan], monkeypatch: pytest.MonkeyPatch
) -> None:
    from botocore.exceptions import ClientError

    from ingest import dispatcher

    started: set[str] = set()

    class FakeSfn:
        def start_execution(self, stateMachineArn: str, name: str, input: str) -> dict[str, str]:
            if name in started:
                raise ClientError(
                    {"Error": {"Code": "ExecutionAlreadyExists", "Message": "x"}}, "StartExecution"
                )
            started.add(name)
            return {"executionArn": name}

    monkeypatch.setattr(aws, "sfn", FakeSfn)
    record = produce("art_1", PAGE)
    outcomes = [dispatcher.start(dispatcher.to_message(record)) for _ in range(3)]

    assert outcomes == ["started", "duplicate", "duplicate"]
    assert len(started) == 1


def test_concurrent_runs_one_wins_the_lease(env: tuple[FakeVectorStore, FakeTitan]) -> None:
    from ingest.steps import claim

    message = message_from(produce("art_1", PAGE))
    first = claim.handler({"ctx": {"message": message, "run_id": new_run_id()}}, FakeContext())
    second = claim.handler({"ctx": {"message": message, "run_id": new_run_id()}}, FakeContext())

    assert (first["action"], second["action"]) == ("RUN", "STALE")
    # The loser can't move LATEST either.
    from ingest.steps import fetch

    with pytest.raises(IngestStale):
        fetch.handler(
            {"ctx": second | {"doc_version": 1, "staging_prefix": "art_1/v1/"}}, FakeContext()
        )
    assert latest("art_1")["run_id"] == first["run_id"]
    assert keys.LATEST == "LATEST"


def test_mark_failed_handles_a_failed_claim(env: tuple[FakeVectorStore, FakeTitan]) -> None:
    from ingest.steps import mark_failed

    message = message_from(produce("art_1", PAGE))
    cause = json.dumps({"errorMessage": json.dumps({"code": "E_BUG", "detail": "claim crashed"})})

    out = mark_failed.handler(
        {
            "ctx": {"message": message, "run_id": new_run_id()},
            "error": {"Error": "IngestPermanent", "Cause": cause},
        },
        FakeContext(),
    )

    assert out["outcome"] == "STALE"  # the run never owned the article, so nothing to mark


def test_freshness_lag_is_measured_per_trigger(
    env: tuple[FakeVectorStore, FakeTitan], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingest import telemetry

    recorded: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        telemetry, "emit", lambda name, value, unit=None, **dims: recorded.append((name, dims))
    )
    record = produce("art_1", PAGE)
    run(message_from(record))
    run(message_from(record, trigger="MANUAL", force=True))

    # Every indexed run reports its lag; the SLO alarm only watches Trigger=STREAM, since a
    # replay of an old article measures its age, not pipeline lag.
    assert [d for n, d in recorded if n == "FreshnessLagSeconds"] == [
        {"Trigger": "STREAM"},
        {"Trigger": "MANUAL"},
    ]


def test_embed_failure_keeps_the_finished_vectors_cached(
    env: tuple[FakeVectorStore, FakeTitan], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, titan = env
    body = (
        "<html><body><main>"
        + "".join(
            f"<h2>Part {i}</h2><p>{f'Distinct sentence number {i} about topic {i}. ' * 40}</p>"
            for i in range(6)
        )
        + "</main></body></html>"
    )
    record = produce("art_emb", body)
    real = titan.embed
    seen: list[str] = []

    def flaky(text: str) -> Embedding:
        seen.append(text)
        if len(seen) == 3:
            raise RuntimeError("Bedrock 500")
        return real(text)

    monkeypatch.setattr(titan, "embed", flaky)
    _, first = run(message_from(record))
    assert first["action"] == "FAILED"
    calls_first = len(seen)
    assert calls_first > 3

    monkeypatch.setattr(titan, "embed", real)
    titan.calls = 0
    _, second = run(message_from(record, force=True, trigger="MANUAL"))

    assert second["action"] == "DONE"
    assert titan.calls == 1  # only the chunk whose call failed


def test_embed_sends_heartbeats_on_long_documents(
    env: tuple[FakeVectorStore, FakeTitan], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingest.status.store import StatusStore
    from ingest.steps import embed as embed_step

    monkeypatch.setattr(embed_step, "CACHE_FLUSH_EVERY", 1)
    monkeypatch.setenv("LEASE_SECONDS", "0")
    from ingest.config import get_settings

    get_settings.cache_clear()
    touches: list[str] = []
    real = StatusStore.touch_lease

    def spy(self: StatusStore, ref: Any, step: StepName) -> None:
        touches.append(step.value)
        real(self, ref, step)

    monkeypatch.setattr(StatusStore, "touch_lease", spy)
    body = (
        "<html><body><main>"
        + "".join(
            f"<h2>S{i}</h2><p>{f'Words about section {i} go here. ' * 30}</p>" for i in range(4)
        )
        + "</main></body></html>"
    )

    _, out = run(message_from(produce("art_beat", body)))

    get_settings.cache_clear()
    assert out["action"] == "DONE"
    assert touches
    assert set(touches) == {"EMBED"}


def test_a_retried_finalize_reannounces_without_undoing_the_run(
    env: tuple[FakeVectorStore, FakeTitan], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingest import events
    from ingest.steps import finalize, mark_failed

    sent: list[dict[str, Any]] = []
    calls = [0]

    def flaky_emit(detail_type: str, detail: dict[str, Any]) -> None:
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("PutEvents throttled")
        sent.append(detail)

    monkeypatch.setattr(events, "emit", flaky_emit)
    record = produce("art_fin", "<html><body><main><p>Finalize twice.</p></main></body></html>")
    run_id, out = run(message_from(record))
    assert out["action"] == "FAILED"  # the first Finalize finished the run, then emit failed
    assert out["outcome"] == "INDEXED"  # MarkFailed saw the finished run and left it alone
    assert latest("art_fin")["status"] == "INDEXED"

    ctx = {
        "article_id": "art_fin",
        "run_id": run_id,
        "doc_version": 1,
        "staging_prefix": "art_fin/v1/",
        "mode": "UPSERT",
        "message": message_from(record),
    }
    finalize.handler({"ctx": ctx}, FakeContext())  # Step Functions retries Finalize

    assert [d["run_id"] for d in sent] == [run_id]
    assert latest("art_fin")["status"] == "INDEXED"
    assert summary("prc_A") == {"indexed": 1, "skipped": 0, "failed": 0}
    again = mark_failed.handler(
        {"ctx": ctx, "error": {"Error": "IngestPermanent", "Cause": "{}"}}, FakeContext()
    )
    assert again["outcome"] == "INDEXED"


def _queued() -> list[dict[str, Any]]:
    sqs = boto3.client("sqs", region_name=REGION)
    url = aws.get_settings().ingest_queue_url
    out: list[dict[str, Any]] = []
    while messages := sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10).get("Messages"):
        for m in messages:
            out.append(json.loads(m["Body"]))
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
    return out


def test_a_delete_during_a_run_is_never_dropped(env: tuple[FakeVectorStore, FakeTitan]) -> None:
    from ingest.steps import claim

    store, _ = env
    record = produce("art_del", PAGE)
    run_id = new_run_id()
    ctx = claim.handler({"ctx": {"message": message_from(record), "run_id": run_id}}, FakeContext())
    handlers()[StepName.FETCH]({"ctx": ctx}, FakeContext())
    remove = {
        "eventName": "REMOVE",
        "dynamodb": {
            "OldImage": {
                "article_id": {"S": "art_del"},
                "updated_at": {"S": "2026-10-11T00:00:00.000Z"},
            }
        },
    }
    _, deferred = run(message_from(remove))  # arrives while the upsert holds the article
    assert deferred["action"] == "DONE"

    with pytest.raises(IngestStale):  # the run meets the marker and hands over
        handlers()[StepName.EXTRACT]({"ctx": ctx}, FakeContext())

    handed = _queued()
    assert [(m["article_id"], m["event"]) for m in handed] == [("art_del", "REMOVE")]
    _, out = run(handed[0])
    assert out["action"] == "DONE"
    assert latest("art_del")["status"] == "DELETED"
    assert store.active("art_del") == []

    _, late = run(message_from(record))  # a redelivery of the old upsert
    assert late["action"] == "SKIP"
    assert latest("art_del")["status"] == "DELETED"


def test_a_late_index_of_a_deleted_version_is_refused(
    env: tuple[FakeVectorStore, FakeTitan],
) -> None:
    store, _ = env
    run(message_from(produce("art_t", PAGE)))
    store.delete_article("art_t", 1)

    with pytest.raises(IngestStale):
        store.write_and_swap("art_t", 1, [])


def test_the_tenant_comes_from_the_producer_row_not_metadata(
    env: tuple[FakeVectorStore, FakeTitan],
) -> None:
    record = produce("art_ten", PAGE)
    boto3.resource("dynamodb", region_name=REGION).Table("articles").update_item(
        Key={"article_id": "art_ten"},
        UpdateExpression="SET tenant_id = :t, metadata = :m",
        ExpressionAttributeValues={":t": "t-news", ":m": {"tenant_id": "t-forged"}},
    )

    run(message_from(record))

    doc = boto3.client("s3", region_name=REGION).get_object(
        Bucket=aws.get_settings().staging_bucket, Key="art_ten/v1/normalized/document.json"
    )
    assert json.loads(doc["Body"].read())["tenant_id"] == "t-news"


# -- metadata-only changes (review M3.1) -----------------------------------------------------


def test_a_metadata_change_updates_the_rows_in_place(
    env: tuple[FakeVectorStore, FakeTitan],
) -> None:
    store, titan = env
    run(message_from(produce("art_m", PAGE)))
    calls, version = titan.calls, latest("art_m")["doc_version"]

    patched = produce(
        "art_m",
        PAGE,
        updated_at="2026-10-11T01:00:00.000Z",
        metadata={"lang": "de", "published_at": "2026-09-30T10:00:00Z"},
        tenant_id="t-news",
    )
    run_id, out = run(message_from(patched))

    assert out["action"] == "DONE"
    item = latest("art_m")
    assert (item["status"], item["doc_version"]) == ("INDEXED", version)  # no new version
    assert titan.calls == calls  # no Bedrock
    rows = store.active("art_m")
    assert rows
    assert {r["tenant_id"] for r in rows} == {"t-news"}
    assert {r["published_at"] for r in rows} == {"2026-09-30"}
    assert all(r["metadata"]["article"]["lang"] == "de" for r in rows)
    items = run_items("art_m", run_id)
    assert len(items) == 2  # RUN# + the Index step
    run_item = next(i for i in items if "#STEP#" not in i["sk"])
    assert (run_item["claim"], run_item["outcome"]) == ("METADATA", "METADATA_UPDATED")
    # Counted like an unchanged article: no change for a process it already has an outcome in.
    assert summary("prc_A") == {"indexed": 1, "skipped": 0, "failed": 0}

    # The same metadata again is unchanged.
    again = produce(
        "art_m",
        PAGE,
        updated_at="2026-10-11T02:00:00.000Z",
        metadata={"lang": "de", "published_at": "2026-09-30T10:00:00Z"},
        tenant_id="t-news",
    )
    assert run(message_from(again))[1]["action"] == "SKIP"
    assert store.metadata_updates == 1


def test_a_metadata_change_in_another_process_counts_as_skipped(
    env: tuple[FakeVectorStore, FakeTitan],
) -> None:
    run(message_from(produce("art_m", PAGE)))
    moved = produce(
        "art_m", PAGE, process_id="prc_B", updated_at="2026-10-11T01:00:00.000Z",
        metadata={"lang": "fr"},
    )  # fmt: skip
    _, out = run(message_from(moved))

    assert out["action"] == "DONE"
    assert summary("prc_B") == {"indexed": 0, "skipped": 1, "failed": 0}


def test_a_new_title_is_a_content_change(env: tuple[FakeVectorStore, FakeTitan]) -> None:
    store, _ = env
    run(message_from(produce("art_t", PAGE)))

    renamed = produce("art_t", PAGE, updated_at="2026-10-11T01:00:00.000Z", title="New title")
    run_id, out = run(message_from(renamed))

    assert out["action"] == "DONE"
    assert latest("art_t")["doc_version"] == 2
    run_item = next(i for i in run_items("art_t", run_id) if "#STEP#" not in i["sk"])
    assert run_item["claim"] == "CHANGED"
    assert len(run_items("art_t", run_id)) == 9  # the whole pipeline
    assert {r["doc_version"] for r in store.active("art_t")} == {2}
    assert all("New title" in r["metadata"]["title"] for r in store.active("art_t"))


def test_articles_indexed_before_fingerprints_get_one_metadata_refresh(
    env: tuple[FakeVectorStore, FakeTitan],
) -> None:
    store, _ = env
    run(message_from(produce("art_o", PAGE)))
    aws.status_store().table.update_item(
        Key=keys.latest_key("art_o"), UpdateExpression="REMOVE meta_sha256, title_sha256"
    )

    again = produce("art_o", PAGE, updated_at="2026-10-11T01:00:00.000Z")
    assert run(message_from(again))[1]["action"] == "DONE"
    assert store.metadata_updates == 1
    assert "meta_sha256" in latest("art_o")

    later = produce("art_o", PAGE, updated_at="2026-10-11T02:00:00.000Z")
    assert run(message_from(later))[1]["action"] == "SKIP"


def test_articles_without_a_tenant_are_indexed_as_default(
    env: tuple[FakeVectorStore, FakeTitan],
) -> None:
    store, _ = env
    run(message_from(produce("art_d", PAGE)))
    assert {r["tenant_id"] for r in store.active("art_d")} == {"default"}
