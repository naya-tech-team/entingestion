from __future__ import annotations

import pytest
from pydantic import ValidationError

from ingest.models import ArticleStatus, IngestMessage, StepName
from ingest.models.status import STEP_STATUS, STEP_SUCCESS_STATUS, TERMINAL_STATUSES

SHA = "a" * 64


def upsert(**overrides: object) -> IngestMessage:
    data: dict[str, object] = {
        "article_id": "art_1",
        "process_id": "prc_1",
        "event": "UPSERT",
        "s3_uri": "s3://raw/raw/art_1/upl_1/doc.pdf",
        "s3_version_id": "v1",
        "content_sha256": SHA,
        "source_updated_at": "2026-10-11T00:00:00.000Z",
        "trigger": "STREAM",
        **overrides,
    }
    return IngestMessage.model_validate(data)


def test_valid_upsert_and_remove() -> None:
    assert upsert().priority == "normal"
    remove = IngestMessage(article_id="art_1", event="REMOVE", trigger="STREAM")
    assert remove.process_id is None
    assert not remove.is_replay


@pytest.mark.parametrize("missing", ["process_id", "s3_uri", "content_sha256"])
def test_upsert_requires_source_fields(missing: str) -> None:
    with pytest.raises(ValidationError, match=missing):
        upsert(**{missing: None})


@pytest.mark.parametrize(
    "overrides",
    [
        {"content_sha256": "A" * 64},
        {"content_sha256": "abc"},
        {"s3_uri": "https://bucket/key"},
        {"trigger": "CRON"},
        {"unknown": 1},
        {"event": "REMOVE", "from_stage": "INDEX"},
    ],
)
def test_invalid_messages(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        upsert(**overrides)


def test_replay_flags() -> None:
    assert upsert(force=True).is_replay
    assert upsert(from_stage="EMBED").is_replay
    assert upsert(from_stage="EMBED").from_stage is StepName.EMBED


def test_step_numbering_and_item_keys() -> None:
    assert [s.number for s in StepName] == list(range(1, 10))
    assert StepName.EXTRACT.item_suffix == "STEP#03_EXTRACT"
    assert StepName.CLAIM.previous is None
    assert StepName.INDEX.previous is StepName.VALIDATE


def test_every_step_has_a_latest_status() -> None:
    assert set(STEP_STATUS) == set(StepName)
    assert STEP_STATUS[StepName.VALIDATE] is ArticleStatus.VALIDATING
    assert STEP_SUCCESS_STATUS[StepName.VALIDATE] is ArticleStatus.STAGED
    assert ArticleStatus.INDEXED in TERMINAL_STATUSES
    assert ArticleStatus.FAILED_RETRYABLE not in TERMINAL_STATUSES
