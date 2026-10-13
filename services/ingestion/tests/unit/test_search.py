"""POST /v1/search in process: authorization from the producer's source access policy (SSM),
the query embedding, the query function call, and optional reranking (faked)."""

from __future__ import annotations

import json
from typing import Any

import boto3
import pytest

from ingest import access, aws
from ingest import search as searching_module
from tests.conftest import REGION
from tests.unit.test_operations import api
from tests.unit.test_pipeline import FakeTitan, FakeVectorStore
from tests.unit.test_quality import FakeLambda

Env = tuple[FakeVectorStore, FakeTitan]
PARAMETER = "/entingestion/test/producer/source_access"
ACCOUNT = "123456789012"
READER = f"arn:aws:iam::{ACCOUNT}:user/search-app"
POLICY = {
    "sources": {
        "news": {"tenant_id": "t-news", "writers": [], "readers": [READER]},
        "wire": {
            "tenant_id": "t-news",
            "writers": [f"arn:aws:iam::{ACCOUNT}:role/wire-*"],
            "readers": [],
        },
        "hr": {"tenant_id": "t-hr", "writers": [], "readers": []},
    },
    "admins": [f"arn:aws:iam::{ACCOUNT}:role/admin"],
}


def hit(chunk: str, text: str = "words") -> dict[str, Any]:
    return {"chunk_id": chunk, "article_id": f"art_{chunk}", "text": text, "score": 0.01}


@pytest.fixture
def searching(env: Env, monkeypatch: pytest.MonkeyPatch) -> FakeLambda:
    boto3.client("ssm", region_name=REGION).put_parameter(
        Name=PARAMETER, Value=json.dumps(POLICY), Type="String"
    )
    monkeypatch.setenv("SOURCE_ACCESS_PARAMETER", PARAMETER)
    monkeypatch.setenv("QUERY_FUNCTION_NAME", "ingestion-test-query")
    monkeypatch.setenv("RERANK_MODEL_ARN", f"arn:aws:bedrock:{REGION}::foundation-model/rerank")
    aws.reset()
    access.reset()
    titan = FakeTitan()
    monkeypatch.setattr(searching_module, "make_embedder", lambda profile: titan)
    fake = FakeLambda(
        {"search_hybrid": lambda e: {"results": [hit(str(i)) for i in range(e["k"])]}}
    )
    monkeypatch.setattr(aws, "lambda_client", lambda: fake)
    return fake


BODY = {"tenant_id": "t-news", "query": "election results", "k": 3}


def test_a_reader_of_a_source_searches_its_tenant(searching: FakeLambda) -> None:
    status, out = api(
        "POST",
        "/v1/search",
        BODY | {"filters": {"source": "news", "published_after": "2026-01-01"}},
        caller=READER,
    )

    assert status == 200
    assert (out["tenant_id"], out["table"], out["reranked"]) == ("t-news", "chunks_primary",
                                                                   False)  # fmt: skip
    assert [h["chunk_id"] for h in out["results"]] == ["0", "1", "2"]
    [call] = searching.calls
    assert call["action"] == "search_hybrid"
    assert (call["tenant_id"], call["text"], call["k"]) == ("t-news", "election results", 3)
    assert call["filters"] == {"source": "news", "published_after": "2026-01-01"}
    assert len(call["vector"]) == 1024


@pytest.mark.parametrize(
    ("caller", "tenant", "allowed"),
    [
        (READER, "t-hr", False),  # reads news, not hr
        (f"arn:aws:sts::{ACCOUNT}:assumed-role/wire-ingest/session-1", "t-news", True),
        (f"arn:aws:sts::{ACCOUNT}:assumed-role/admin/ops", "t-hr", True),
        (f"arn:aws:iam::{ACCOUNT}:user/someone-else", "t-news", False),
    ],
)
def test_the_tenant_is_authorized_from_the_source_policy(
    searching: FakeLambda, caller: str, tenant: str, allowed: bool
) -> None:
    status, _ = api("POST", "/v1/search", BODY | {"tenant_id": tenant}, caller=caller)
    assert status == (200 if allowed else 403)
    assert bool(searching.calls) is allowed  # a refused caller never reaches the database


def test_without_a_policy_nobody_searches(searching: FakeLambda, monkeypatch: Any) -> None:
    monkeypatch.setenv("SOURCE_ACCESS_PARAMETER", "")
    aws.reset()
    access.reset()
    assert api("POST", "/v1/search", BODY, caller=READER)[0] == 403


@pytest.mark.parametrize(
    "body",
    [
        BODY | {"k": 51},
        BODY | {"query": "   "},
        BODY | {"filters": {"source": "Bad Source!"}},
        BODY | {"filters": {"published_after": "yesterday"}},
        BODY | {"tenant": "t-news"},
        {"query": "x"},
    ],
)
def test_bad_search_requests(searching: FakeLambda, body: dict[str, Any]) -> None:
    assert api("POST", "/v1/search", body, caller=READER)[0] == 422


class FakeReranker:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def rerank(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("throttled")
        n = kwargs["rerankingConfiguration"]["bedrockRerankingConfiguration"]["numberOfResults"]
        count = len(kwargs["sources"])
        return {
            "results": [{"index": count - 1 - i, "relevanceScore": 1.0 - i / 10} for i in range(n)]
        }


def test_rerank_reorders_a_larger_pool_when_enabled(
    searching: FakeLambda, monkeypatch: pytest.MonkeyPatch
) -> None:
    reranker = FakeReranker()
    monkeypatch.setattr(aws, "bedrock_agent", lambda: reranker)

    # Off (the default): the flag in the request is ignored.
    _, out = api("POST", "/v1/search", BODY | {"rerank": True}, caller=READER)
    assert out["reranked"] is False
    assert reranker.calls == []

    monkeypatch.setenv("RERANK_ENABLED", "true")
    aws.reset()
    _, out = api("POST", "/v1/search", BODY | {"rerank": True}, caller=READER)

    assert out["reranked"] is True
    assert searching.calls[-1]["k"] == 6  # 2 candidates per wanted result
    assert [h["chunk_id"] for h in out["results"]] == ["5", "4", "3"]
    assert out["results"][0]["rerank_score"] == 1.0
    config = reranker.calls[0]["rerankingConfiguration"]["bedrockRerankingConfiguration"]
    assert config["modelConfiguration"]["modelArn"].endswith("/rerank")


def test_a_rerank_failure_keeps_the_fused_order(
    searching: FakeLambda, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RERANK_ENABLED", "true")
    aws.reset()
    monkeypatch.setattr(aws, "bedrock_agent", lambda: FakeReranker(fail=True))

    status, out = api("POST", "/v1/search", BODY | {"rerank": True}, caller=READER)

    assert status == 200
    assert out["reranked"] is False
    assert [h["chunk_id"] for h in out["results"]] == ["0", "1", "2"]
