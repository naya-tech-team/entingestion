from __future__ import annotations

import re

from ingest.ids import (
    EXECUTION_NAME_MAX,
    chunk_id,
    embedding_cache_key,
    execution_name,
    new_run_id,
    staging_prefix,
)

ARTICLE = "art_" + "0123456789abcdef" * 2
SHA = "b" * 64
KEY = {
    "content_sha256": SHA,
    "process_id": "prc_01M4M696B6W2ADRHZSPX135M5Z",
    "source_updated_at": "2026-10-11T00:00:00.000Z",
    "pipeline_version": "v1",
}


def test_execution_name_is_valid_for_step_functions() -> None:
    name = execution_name(ARTICLE, **KEY, replay_run_id=new_run_id())
    assert len(name) <= EXECUTION_NAME_MAX
    assert re.fullmatch(r"[A-Za-z0-9_-]+", name)  # no '#', which Step Functions rejects


def test_same_record_gives_same_name() -> None:
    # The same stream record delivered 3x -> one execution (ExecutionAlreadyExists).
    assert execution_name(ARTICLE, **KEY) == execution_name(ARTICLE, **KEY)


def test_new_process_or_patch_gets_its_own_run() -> None:
    base = execution_name(ARTICLE, **KEY)
    other_process = execution_name(ARTICLE, **(KEY | {"process_id": "prc_other"}))
    patched = execution_name(ARTICLE, **(KEY | {"source_updated_at": "2026-10-11T01:00:00.000Z"}))
    new_pipeline = execution_name(ARTICLE, **(KEY | {"pipeline_version": "v2"}))
    assert len({base, other_process, patched, new_pipeline}) == 4


def test_replay_suffix() -> None:
    run = new_run_id()
    assert execution_name(ARTICLE, **KEY, replay_run_id=run).endswith(f"-r{run[-8:]}")


def test_chunk_and_cache_keys_are_deterministic() -> None:
    assert chunk_id("art_1", 2, 0) == chunk_id("art_1", 2, 0)
    assert chunk_id("art_1", 2, 0) != chunk_id("art_1", 3, 0)
    assert embedding_cache_key("text", "m", 512, True).endswith("#m@512@norm")
    assert embedding_cache_key("text", "m", 512, False).endswith("#m@512@raw")
    assert staging_prefix("art_1", 3) == "art_1/v3/"
