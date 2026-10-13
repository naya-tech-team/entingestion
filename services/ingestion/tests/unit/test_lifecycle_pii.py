"""What a delete leaves behind (staging, embedding cache), old versions' staged files, and PII
screening in Normalize (Comprehend faked)."""

from __future__ import annotations

import json
import re
from typing import Any

import boto3
import pytest

from ingest import access, aws, pii
from ingest.vectorstore.targets import TARGETS
from tests.conftest import REGION, STAGING_BUCKET
from tests.unit.test_pipeline import (
    PAGE,
    FakeTitan,
    FakeVectorStore,
    latest,
    message_from,
    produce,
    run,
    summary,
)

Env = tuple[FakeVectorStore, FakeTitan]


def staged_versions(prefix: str) -> list[str]:
    resp = boto3.client("s3", region_name=REGION).list_object_versions(
        Bucket=STAGING_BUCKET, Prefix=prefix
    )
    return [v["Key"] for v in resp.get("Versions", []) + resp.get("DeleteMarkers", [])]


def current_keys(prefix: str) -> list[str]:
    resp = boto3.client("s3", region_name=REGION).list_objects_v2(
        Bucket=STAGING_BUCKET, Prefix=prefix
    )
    return [o["Key"] for o in resp.get("Contents", [])]


CHANGED = PAGE.replace("</article>", "<h2>Extra</h2><p>A brand new closing section.</p></article>")


# -- lifecycle ---------------------------------------------------------------------------------


def test_a_new_version_retires_the_old_versions_staged_files(env: Env) -> None:
    run(message_from(produce("art_1", PAGE)))
    run(message_from(produce("art_1", CHANGED, updated_at="2026-10-11T01:00:00.000Z")))

    assert latest("art_1")["doc_version"] == 2
    assert current_keys("art_1/v1/") == []  # noncurrent now: the lifecycle expires them
    assert any(k.startswith("art_1/v1/") for k in staged_versions("art_1/v1/"))
    assert "art_1/v2/chunks/chunks.jsonl" in current_keys("art_1/v2/")  # the rebuild source


def test_a_delete_purges_staging_and_the_embedding_cache(env: Env) -> None:
    run(message_from(produce("art_1", PAGE)))
    run(message_from(produce("art_1", CHANGED, updated_at="2026-10-11T01:00:00.000Z")))
    run(message_from(produce("art_10", PAGE)))  # its prefix must survive
    chunks = [
        json.loads(line)
        for line in aws.s3()
        .get_object(Bucket=STAGING_BUCKET, Key="art_1/v2/chunks/chunks.jsonl")["Body"]
        .read()
        .decode()
        .splitlines()
    ]
    live_key = TARGETS["chunks_primary"].cache_key(chunks[-1]["embed_text"])
    cache = aws.table(aws.get_settings().cache_table)
    assert "Item" in cache.get_item(Key={"cache_key": live_key})

    record = {"eventName": "REMOVE", "dynamodb": {"Keys": {"article_id": {"S": "art_1"}}}}
    _, out = run(message_from(record))

    assert out["action"] == "DONE"
    assert latest("art_1")["status"] == "DELETED"
    assert staged_versions("art_1/") == []  # every version of every object, for good
    assert staged_versions("art_10/")
    assert "Item" not in cache.get_item(Key={"cache_key": live_key})


# -- PII screening -----------------------------------------------------------------------------

EMAIL = re.compile(r"[\w.]+@[\w.]+\w")
LEAKY = PAGE.replace("Content sentence number 1 is here.", "Mail jane.doe@example.com today.", 1)


class FakeComprehend:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def detect_pii_entities(self, Text: str, LanguageCode: str) -> dict[str, Any]:
        self.requests.append(Text)
        return {
            "Entities": [
                {"Type": "EMAIL", "Score": 0.99, "BeginOffset": m.start(), "EndOffset": m.end()}
                for m in EMAIL.finditer(Text)
            ]
            + [{"Type": "NAME", "Score": 0.2, "BeginOffset": 0, "EndOffset": 4}]  # below min
        }


@pytest.fixture
def screening(env: Env, monkeypatch: pytest.MonkeyPatch) -> FakeComprehend:
    fake = FakeComprehend()
    monkeypatch.setattr(aws, "comprehend", lambda: fake)
    monkeypatch.setenv("PII_SCREENING", "true")
    monkeypatch.setenv("SOURCE_ACCESS_PARAMETER", "/test/source_access")
    aws.reset()
    access.reset()
    return fake


def set_policy(pii_policy: str) -> None:
    value = {"sources": {"news": {"tenant_id": "t", "writers": [], "pii": pii_policy}}}
    boto3.client("ssm", region_name=REGION).put_parameter(
        Name="/test/source_access", Value=json.dumps(value), Type="String", Overwrite=True
    )
    access.reset()


def normalized(article_id: str) -> dict[str, Any]:
    body = aws.s3().get_object(
        Bucket=STAGING_BUCKET, Key=f"{article_id}/v1/normalized/document.json"
    )["Body"]
    return dict(json.loads(body.read()))


def test_redact_replaces_entities_before_chunking(screening: FakeComprehend, env: Env) -> None:
    set_policy("redact")
    run(message_from(produce("art_p", LEAKY)))

    doc = normalized("art_p")
    text = " ".join(b["text"] for b in doc["blocks"])
    assert "jane.doe@example.com" not in text
    assert "Mail [EMAIL] today." in text
    assert doc["pii"] == {"count": 1, "types": {"EMAIL": 1}, "action": "redact"}
    item = latest("art_p")
    assert (item["status"], item["pii_count"], item["pii_types"]) == ("INDEXED", 1, ["EMAIL"])
    assert len(screening.requests) == 1  # one request for the whole article


def test_block_skips_the_article(screening: FakeComprehend, env: Env) -> None:
    set_policy("block")
    run(message_from(produce("art_p", LEAKY)))

    item = latest("art_p")
    assert (item["status"], item["skip_reason"]) == ("SKIPPED", "E_PII_BLOCKED")
    assert summary("prc_A")["skipped"] == 1
    store, _ = env
    assert store.active("art_p") == []


def test_allow_indexes_as_is_and_records_the_finding(screening: FakeComprehend, env: Env) -> None:
    set_policy("allow")
    run(message_from(produce("art_p", LEAKY)))

    doc = normalized("art_p")
    assert any("jane.doe@example.com" in b["text"] for b in doc["blocks"])
    assert doc["pii"]["action"] == "allow"
    assert latest("art_p")["status"] == "INDEXED"


def test_screening_is_off_by_default(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aws, "comprehend", lambda: pytest.fail("Comprehend called"))
    run(message_from(produce("art_p", LEAKY)))
    assert normalized("art_p")["pii"] is None


def test_large_texts_are_batched_and_offsets_mapped_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pii, "MAX_BYTES", 64)
    fake = FakeComprehend()
    texts = ["x" * 50 + " a@b.co", "short y@z.io text", "", "c@d.ee"]

    found = pii.screen(texts, pii.comprehend_detector(fake), 0.5)

    assert len(fake.requests) > 1
    assert all(len(r.encode()) <= 64 + 16 for r in fake.requests)
    assert [[texts[i][s.start : s.end] for s in spans] for i, spans in enumerate(found.spans)] == [
        ["a@b.co"],
        ["y@z.io"],
        [],
        ["c@d.ee"],
    ]
    assert pii.redact(texts[1], found.spans[1]) == "short [EMAIL] text"
