"""Per-tenant vector-table registries: a tenant with its own registry (TENANT_PROFILES, or its
first change) is written, searched, reindexed, switched and drift-checked on its own tables; every
other tenant keeps the default registry, which behaves as before."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ingest import aws
from ingest.models.status import StepName
from tests.unit.test_operations import api
from tests.unit.test_pipeline import (
    PAGE,
    FakeTitan,
    FakeVectorStore,
    latest,
    message_from,
    produce,
    run,
)
from tests.unit.test_quality import FakeLambda, backfill_queued
from tests.unit.test_reindex import FakeSfn, _TablesFake, put_report, reindex_message

Env = tuple[FakeVectorStore, FakeTitan]
OLD, NEW = "chunks_primary", "chunks_compact"


@pytest.fixture
def mapped(env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    """Tenant t-small starts on the 512-d profile; every other tenant uses the default."""
    monkeypatch.setenv("TENANT_PROFILES", json.dumps({"t-small": "compact"}))
    aws.reset()
    return env


def tables(env: Env) -> dict[str, FakeVectorStore]:
    return env[0].tables  # type: ignore[attr-defined, no-any-return]


def rows_of(env: Env, table: str, article_id: str) -> list[dict[str, Any]]:
    return [r for r in tables(env)[table].rows.values() if r["article_id"] == article_id]


def store() -> Any:
    return aws.status_store()


# -- the registry ----------------------------------------------------------------------------


def test_registries_by_tenant(mapped: Env) -> None:
    from ingest.vectorstore import registry

    default = registry.load(store())
    assert (default.live, default.writes, default.tenant) == (OLD, (OLD,), None)
    assert registry.load(store(), "default") == default
    assert registry.load(store(), "t-other") == default  # no registry of its own
    small = registry.load(store(), "t-small")
    assert (small.live, small.writes, small.tenant, small.epoch) == (NEW, (NEW,), "t-small", 0)
    assert small.as_dict()["tenant_id"] == "t-small"
    assert "tenant_id" not in default.as_dict()  # the default registry looks as it did

    # A tenant's first change stores its own registry, copied from the default one.
    moved = registry.add_write(store(), NEW, "op", "t-big")
    assert (moved.live, moved.writes, moved.tenant, moved.epoch) == (OLD, (OLD, NEW), "t-big", 1)
    assert registry.load(store()).writes == (OLD,)  # the default registry is unchanged
    assert set(registry.load_all(store())) == {None, "t-small", "t-big"}
    assert store().get_config("VECTOR_TABLES#t-big")["writes"] == [OLD, NEW]

    registry.set_live(store(), NEW, "op", "t-big")
    assert registry.load(store(), "t-big").live == NEW
    assert registry.remove_write(store(), OLD, "op", "t-big").writes == (NEW,)
    with pytest.raises(registry.RegistryError):
        registry.remove_write(store(), NEW, "op", "t-big")


def test_a_concurrent_first_change_is_refused(mapped: Env) -> None:
    from ingest.errors import IngestTransient
    from ingest.vectorstore import registry

    default = registry.load(store())
    registry.add_write(store(), NEW, "op", "t-big")
    new = registry.VectorTables(OLD, (OLD, NEW), default.epoch + 1, "t-big")
    with pytest.raises(IngestTransient):  # E_DB_CONFLICT: the tenant's item exists by now
        registry._save(store(), default, new, "op")


# -- live runs write their tenant's tables ---------------------------------------------------


def test_a_mapped_tenants_articles_go_to_its_table_only(mapped: Env) -> None:
    run(message_from(produce("art_s", PAGE, tenant_id="t-small")))
    run(message_from(produce("art_d", PAGE + " ", tenant_id="t-other")))

    assert rows_of(mapped, NEW, "art_s")
    assert not rows_of(mapped, OLD, "art_s")
    assert rows_of(mapped, OLD, "art_d")
    assert not rows_of(mapped, NEW, "art_d")
    assert {r["tenant_id"] for r in rows_of(mapped, NEW, "art_s")} == {"t-small"}
    item = latest("art_s")
    assert (item["tenant_id"], item[f"tv_{NEW}"]) == ("t-small", 1)
    assert f"tv_{OLD}" not in item
    assert latest("art_d")["tenant_id"] == "t-other"


def test_messages_carry_the_rows_tenant(mapped: Env) -> None:
    from ingest import requeue
    from ingest.dispatcher import to_message

    record = produce("art_t", PAGE, tenant_id="t-small")
    assert to_message(record).tenant_id == "t-small"
    message = requeue.message_for(store(), "art_t", trigger="MANUAL")
    assert message is not None
    assert message.tenant_id == "t-small"
    assert to_message(produce("art_u", PAGE)).tenant_id is None


def test_an_article_moving_to_a_tenant_with_other_tables_is_reembedded(mapped: Env) -> None:
    run(message_from(produce("art_m", PAGE, tenant_id="t-other")))
    assert rows_of(mapped, OLD, "art_m")

    # Same content, but now the tenant's registry writes chunks_compact: a full run (not a
    # metadata update, which would only touch the old tables).
    _, out = run(
        message_from(
            produce("art_m", PAGE, tenant_id="t-small", updated_at="2026-10-12T00:00:00.000Z")
        )
    )

    assert out["action"] == "DONE"
    assert latest("art_m")["doc_version"] == 2
    assert {r["tenant_id"] for r in rows_of(mapped, NEW, "art_m")} == {"t-small"}
    # Its rows in the old table are kept but no longer the old tenant's.
    assert {r["tenant_id"] for r in rows_of(mapped, OLD, "art_m")} == {"t-small"}


def test_a_tenant_change_within_one_registry_is_a_metadata_update(env: Env) -> None:
    run(message_from(produce("art_n", PAGE, tenant_id="t-a")))
    _, out = run(
        message_from(produce("art_n", PAGE, tenant_id="t-b", updated_at="2026-10-12T00:00:00.000Z"))
    )
    assert out["action"] == "DONE"
    assert latest("art_n")["doc_version"] == 1  # nothing re-embedded
    assert {r["tenant_id"] for r in rows_of(env, OLD, "art_n")} == {"t-b"}


# -- control API -----------------------------------------------------------------------------


def test_tables_route_shows_a_tenants_registry(
    mapped: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(aws, "lambda_client", _TablesFake)
    status, body = api("GET", "/v1/tables")
    assert (status, body["registry"]["live"], body["consistent"]) == (200, OLD, True)
    assert body["tenants"] == {"t-small": {"live": NEW, "writes": [NEW], "epoch": 0,
                                           "tenant_id": "t-small"}}  # fmt: skip
    assert body["profiles"]["compact"] == {
        "table": NEW,
        "provider": "bedrock",
        "model": "amazon.titan-embed-text-v2:0",
        "dimension": 512,
    }
    status, body = api("GET", "/v1/tables?tenant_id=t-small")
    assert (body["tenant_id"], body["registry"]["live"]) == ("t-small", NEW)


def test_moving_one_tenant_to_another_model(mapped: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reindex, switch and retire for one tenant: the default registry and chunks_active stay."""
    sfn = FakeSfn()
    monkeypatch.setattr(aws, "sfn", lambda: sfn)
    _TablesFake.calls = []
    monkeypatch.setattr(aws, "lambda_client", _TablesFake)
    big = produce("art_b1", PAGE, tenant_id="t-big")
    run(message_from(big))
    run(message_from(produce("art_o1", PAGE + " ", tenant_id="t-other")))

    status, out = api(
        "POST", "/v1/reindex", {"tenant_id": "t-big", "target_table": NEW, "dry_run": True}
    )
    assert (status, out["count"], out["tenant_id"]) == (200, 1, "t-big")  # only t-big's
    status, out = api("POST", "/v1/reindex", {"tenant_id": "t-big", "target_table": NEW})
    assert status == 202
    assert out["tables"] == {"live": OLD, "writes": [OLD, NEW], "epoch": 1, "tenant_id": "t-big"}
    assert json.loads(sfn.started[0]["input"])["tenant_id"] == "t-big"

    from ingest.reindex import ReindexFilter, plan_items

    assert plan_items(store(), ReindexFilter(), NEW, "t-big") == ["art_b1"]
    assert plan_items(store(), ReindexFilter(), NEW) == ["art_o1"]  # the default registry's

    switch = {"table": NEW, "tenant_id": "t-big"}
    status, body = api("POST", "/v1/tables/active", switch)
    assert (status, body["sample"]) == (409, ["art_b1"])  # t-big's article isn't there yet
    run(reindex_message(big, StepName.EMBED, NEW))
    put_report(OLD, 0.80)
    put_report(NEW, 0.80)
    status, body = api("POST", "/v1/tables/active", switch)
    assert (status, body["active_table"], body["tables"]["live"]) == (200, NEW, NEW)
    assert [c["action"] for c in _TablesFake.calls] == []  # chunks_active untouched

    from ingest.vectorstore import registry

    assert registry.load(store()).live == OLD
    status, body = api("DELETE", f"/v1/tables/writes/{OLD}?tenant_id=t-big")
    assert (status, body["writes"]) == (200, [NEW])
    assert api("DELETE", f"/v1/tables/writes/{NEW}?tenant_id=t-big")[0] == 409

    # The tenant's next edit writes only its new table.
    run(
        message_from(
            produce("art_b1", PAGE + "!", tenant_id="t-big", updated_at="2026-10-12T00:00:00.000Z")
        )
    )
    assert {r["doc_version"] for r in rows_of(mapped, NEW, "art_b1")} == {2}
    assert {r["doc_version"] for r in rows_of(mapped, OLD, "art_b1")} == {1}  # kept: rollback


def test_search_reads_the_tenants_live_table(mapped: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from ingest import search as searching

    seen: list[int] = []

    def embedder(profile: Any) -> FakeTitan:
        seen.append(profile.dimension)
        return FakeTitan(dimension=profile.dimension)

    monkeypatch.setattr(searching, "make_embedder", embedder)
    fake = FakeLambda({"search_hybrid": {"results": []}})
    monkeypatch.setattr(aws, "lambda_client", lambda: fake)

    assert (
        searching.search(tenant_id="t-small", query="q", k=3, filters={}, rerank=False)["table"]
        == NEW
    )
    searching.search(tenant_id="t-other", query="q", k=3, filters={}, rerank=False)

    assert seen == [512, 1024]
    assert [(c["table"], len(c["vector"])) for c in fake.calls] == [(NEW, 512), (OLD, 1024)]
    assert [c["tenant_id"] for c in fake.calls] == ["t-small", "t-other"]


# -- drift -----------------------------------------------------------------------------------


def test_drift_checks_each_article_against_its_registry(
    mapped: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingest.reconciler.app import Reconciler

    run(message_from(produce("art_s", PAGE, tenant_id="t-small")))
    run(message_from(produce("art_d", PAGE + " ", tenant_id="t-other")))
    count = int(latest("art_s")["chunk_count"])
    rows = {
        # art_d's stale rows in a table its registry doesn't write: left alone (rollback).
        NEW: [["art_d", 1, 1], ["art_s", 1, count - 1]],
        OLD: [["art_s", 1, 2]],  # likewise for art_s
    }
    fake = FakeLambda({"versions": lambda e: {"rows": rows.get(e["table"], []), "next": None}})
    monkeypatch.setattr(aws, "lambda_client", lambda: fake)

    counts = Reconciler(aws.status_store(), 2**31, 500).run(drift=True)

    assert {c["table"] for c in fake.calls} == {OLD, NEW}
    repairs = [(m["article_id"], m["target_table"], m["from_stage"]) for m in backfill_queued()]
    assert repairs == [("art_s", NEW, "INDEX")]  # its live table lost a row
    assert counts.get("requeued_drift_orphan", 0) == 0


def test_a_status_item_without_a_tenant_belongs_to_the_default_registry(mapped: Env) -> None:
    from ingest.tenancy import Tenancy

    run(message_from(produce("art_s", PAGE, tenant_id="t-small")))
    item = latest("art_s")
    del item["tenant_id"]
    tenancy = Tenancy(store())
    assert tenancy.tenants([item]) == {"art_s": None}
    assert tenancy.governed([item], "t-small") == []
    assert tenancy.governed([item], None) == [item]
