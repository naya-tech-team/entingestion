"""Vector-table operations: the tables function (status, switch as ingest_ops) and what the
migrate function still does (migrations, truncate unless ALLOW_TRUNCATE is false)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from ingest import migrate, tables
from ingest.config import get_settings
from tests.unit.test_pipeline import FakeContext


class RecordingConn:
    """Records SQL; answers the view-definition query like Postgres would."""

    def __init__(self) -> None:
        self.sql: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, query: Any, params: Any = None) -> RecordingConn:
        self.sql.append(query if isinstance(query, str) else query.as_string(None))
        return self

    def fetchone(self) -> tuple[str]:
        return (" SELECT chunk_id\n   FROM chunks_primary\n  WHERE is_active;",)

    def fetchall(self) -> list[Any]:
        return []

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DB_HOST", "db.test.invalid")  # the tables and query functions need one
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_activate_switches_the_view_as_its_owner() -> None:
    conn = RecordingConn()

    out = migrate.activate(conn, "chunks_compact")  # type: ignore[arg-type]

    assert out == {"previous": "chunks_primary", "active_table": "chunks_compact"}
    switch = conn.sql[1:]
    assert switch[0] == 'SET LOCAL ROLE "ingest_views"'  # first, so the new view keeps its owner
    assert switch[1] == "DROP VIEW IF EXISTS chunks_active"
    assert 'FROM "chunks_compact" WHERE is_active' in switch[2]
    assert "WITH (security_invoker = true)" in switch[2]  # readers stay in their tenant
    assert switch[3].startswith("GRANT SELECT ON chunks_active TO ingest_reader")
    assert conn.commits == 1  # committed even when the read above opened a transaction


def test_tables_function_dispatches_on_a_clean_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = RecordingConn()
    monkeypatch.setattr(tables, "connection", lambda settings: conn)
    monkeypatch.setattr(tables, "status", lambda c: {"active_table": "x", "tables": {}})
    calls: list[str] = []

    def fake_activate(c: object, table: str) -> dict[str, str]:
        calls.append(table)
        return {"active_table": table}

    monkeypatch.setattr(tables, "activate", fake_activate)

    assert tables.handler({"action": "status"}, FakeContext())["active_table"] == "x"
    out = tables.handler({"action": "activate", "table": "chunks_compact"}, FakeContext())

    assert out == {"active_table": "chunks_compact"}
    assert calls == ["chunks_compact"]
    assert conn.rollbacks == 4  # before and after each call: nothing left open when warm


@pytest.mark.parametrize(
    ("event", "error"),
    [
        ({"action": "truncate", "table": "chunks_compact"}, ValueError),  # nothing else
        ({"action": "activate", "table": "nope"}, ValueError),
        ({"action": "migrate"}, ValueError),
    ],
)
def test_tables_function_refuses_other_actions(
    monkeypatch: pytest.MonkeyPatch, event: dict[str, Any], error: type[Exception]
) -> None:
    monkeypatch.setattr(tables, "connection", lambda settings: pytest.fail("no connection"))
    with pytest.raises(error):
        tables.handler(event, FakeContext())


@pytest.mark.parametrize("action", ["status", "activate"])
def test_migrate_no_longer_switches_tables(action: str) -> None:
    with pytest.raises(ValueError, match="tables function"):
        migrate.handler({"action": action, "table": "chunks_compact"}, FakeContext())


def test_truncate_is_refused_unless_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrate.boto3, "client", lambda *a, **k: pytest.fail("no secret read"))
    event = {"action": "truncate", "table": "chunks_compact", "confirm": "chunks_compact"}

    with pytest.raises(PermissionError, match="ALLOW_TRUNCATE"):
        migrate.handler(event, FakeContext())


def test_truncate_needs_confirmation_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_TRUNCATE", "true")
    monkeypatch.setattr(migrate.boto3, "client", lambda *a, **k: pytest.fail("no secret read"))
    with pytest.raises(ValueError, match="confirm"):
        migrate.handler({"action": "truncate", "table": "chunks_compact"}, FakeContext())


def test_switches_are_off_unless_set() -> None:
    settings = get_settings()
    assert not settings.allow_truncate
    assert not settings.ocr.enabled


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("TRUE", True), ("1", True), ("false", False), ("", False)],
)
def test_switches_read_the_same_everywhere(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    for name in ("ALLOW_TRUNCATE", "OCR_ENABLED", "PII_SCREENING", "RERANK_ENABLED"):
        monkeypatch.setenv(name, raw)
    get_settings.cache_clear()
    s = get_settings()
    assert (s.allow_truncate, s.ocr.enabled, s.pii.screening, s.search.rerank_enabled) == (
        expected,
    ) * 4


def test_a_bad_switch_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from ingest.config import MissingSetting

    monkeypatch.setenv("OCR_ENABLED", "maybe")
    get_settings.cache_clear()
    with pytest.raises(MissingSetting, match="OCR_ENABLED must be true or false"):
        get_settings()


# -- query function: which actions see every tenant ----------------------------------------


def test_only_internal_query_actions_read_every_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    from ingest import query

    conn = RecordingConn()
    monkeypatch.setattr(query, "connection", lambda settings: conn)

    query.handler({"action": "counts"}, FakeContext())  # type: ignore[arg-type]
    assert any("app.all_tenants', 'on'" in q for q in conn.sql)

    conn.sql.clear()
    event = {"action": "search_hybrid", "tenant_id": "t-a", "vector": [0.1], "text": "x"}
    assert query.handler(event, FakeContext()) == {"results": []}  # type: ignore[arg-type]
    assert not any("'on'" in q for q in conn.sql)
    assert any("set_config('app.tenant_id'" in q for q in conn.sql)
    assert conn.rollbacks >= 2  # starts clean, ends clean

    with pytest.raises(ValueError, match="tenant"):
        query.handler(event | {"tenant_id": ""}, FakeContext())  # type: ignore[arg-type]


# -- required settings ------------------------------------------------------------------------


def test_a_handler_names_the_settings_it_lacks(monkeypatch: pytest.MonkeyPatch) -> None:
    from ingest import dispatcher, query
    from ingest.config import MissingSetting

    monkeypatch.delenv("DB_HOST")
    monkeypatch.delenv("STATE_MACHINE_ARN", raising=False)
    monkeypatch.delenv("STATUS_TABLE", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(query, "connection", lambda settings: pytest.fail("no connection"))

    with pytest.raises(MissingSetting, match=r"missing settings: db\.host"):
        query.handler({"action": "counts"}, FakeContext())  # type: ignore[arg-type]
    with pytest.raises(MissingSetting, match="status_table, state_machine_arn"):
        dispatcher.handler({"Records": []}, FakeContext())  # type: ignore[arg-type]
