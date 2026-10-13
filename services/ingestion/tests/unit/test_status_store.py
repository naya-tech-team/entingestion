from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from ingest import aws
from ingest.errors import (
    DeletePending,
    ErrorCode,
    IngestPermanent,
    IngestStale,
    IngestThrottled,
    IngestTransient,
)
from ingest.ids import new_run_id
from ingest.models import IngestMessage
from ingest.models.status import ArticleStatus, RunOutcome, StepName
from ingest.status import keys
from ingest.status.store import StatusStore, StepRef

SHA1, SHA2 = "1" * 64, "2" * 64


def msg(sha: str = SHA1, process_id: str = "prc_A", **kw: Any) -> IngestMessage:
    data: dict[str, Any] = {
        "article_id": "art_1",
        "process_id": process_id,
        "event": "UPSERT",
        "s3_uri": "s3://raw/raw/art_1/upl_1/doc.html",
        "content_sha256": sha,
        "source_updated_at": "2026-10-11T00:00:00.000Z",
        "trigger": "STREAM",
        **kw,
    }
    return IngestMessage.model_validate(data)


@pytest.fixture
def store(aws: None) -> StatusStore:
    return aws_store()


def aws_store() -> StatusStore:
    return aws.status_store()


def run_to(
    store: StatusStore, m: IngestMessage, status: ArticleStatus = ArticleStatus.INDEXED
) -> str:
    """Claim and drive one run straight to a terminal status."""
    run_id = new_run_id()
    claim = store.claim(m, run_id, None)
    assert claim.action == "RUN"
    ref = StepRef(m.article_id, run_id, m.process_id)
    outcome = {
        ArticleStatus.INDEXED: RunOutcome.INDEXED,
        ArticleStatus.FAILED_PERMANENT: RunOutcome.FAILED_PERMANENT,
    }[status]
    store.finish(ref, status, outcome)
    return run_id


def summary(store: StatusStore, pid: str) -> dict[str, int]:
    s = store.summary(pid) or {}
    return {k: int(s.get(k, 0)) for k in ("indexed", "skipped", "failed")}


# -- claim -------------------------------------------------------------------


def test_first_claim_creates_latest_and_holds_the_lease(store: StatusStore) -> None:
    run_id = new_run_id()
    result = store.claim(msg(), run_id, "arn:exec")

    assert (result.action, result.doc_version, result.start_at) == ("RUN", 1, StepName.FETCH)
    latest = store.latest("art_1")
    assert latest is not None
    assert latest["status"] == "CLAIMED"
    assert latest["lease_owner"] == run_id
    assert latest["gsi3pk"] == keys.LEASE_BUCKET
    assert latest["staging_prefix"] == "art_1/v1/"
    run = store.get(keys.run_key("art_1", run_id))
    assert run is not None
    assert run["claim"] == "NEW"
    assert run["execution_arn"] == "arn:exec"


def test_live_lease_makes_a_second_run_stale(store: StatusStore) -> None:
    store.claim(msg(), new_run_id(), None)
    other = new_run_id()

    assert store.claim(msg(SHA2), other, None).action == "STALE"
    assert store.get(keys.run_key("art_1", other))["outcome"] == "STALE"  # type: ignore[index]


def test_unchanged_content_is_skipped_and_counted_for_a_new_process(store: StatusStore) -> None:
    run_to(store, msg())
    skip_run = new_run_id()

    result = store.claim(msg(process_id="prc_B"), skip_run, None)

    assert result.action == "SKIP"
    assert store.get(keys.run_key("art_1", skip_run))["outcome"] == "SKIPPED_UNCHANGED"  # type: ignore[index]
    assert store.latest("art_1")["status"] == "INDEXED"  # type: ignore[index]
    assert summary(store, "prc_A") == {"indexed": 1, "skipped": 0, "failed": 0}
    assert summary(store, "prc_B") == {"indexed": 0, "skipped": 1, "failed": 0}
    # Only the RUN# item was written for the skipped run.
    assert len(store.run_items("art_1", skip_run)) == 1


def test_changed_content_bumps_the_version(store: StatusStore) -> None:
    run_to(store, msg())

    result = store.claim(msg(SHA2), new_run_id(), None)

    assert (result.action, result.doc_version, result.staging_prefix) == ("RUN", 2, "art_1/v2/")


def test_replay_from_a_step_reuses_the_version(store: StatusStore) -> None:
    run_to(store, msg())

    result = store.claim(msg(from_stage="EMBED", trigger="MANUAL"), new_run_id(), None)

    assert (result.action, result.doc_version, result.start_at) == ("RUN", 1, StepName.EMBED)
    assert store.latest("art_1")["stage"] == "CHUNK"  # type: ignore[index]


def test_a_retried_claim_repeats_its_decision(store: StatusStore) -> None:
    run_to(store, msg())
    run_id = new_run_id()
    first = store.claim(msg(SHA2), run_id, None)

    again = store.claim(msg(SHA2), run_id, None)  # Step Functions retried after a lost response

    assert again == first
    assert (again.action, again.doc_version) == ("RUN", 2)
    assert store.latest("art_1")["doc_version"] == 2  # type: ignore[index]


META1, META2, TITLE1, TITLE2 = "a" * 64, "b" * 64, "c" * 64, "d" * 64


def test_a_metadata_change_claims_a_metadata_run(store: StatusStore) -> None:
    run_to(store, msg(meta_sha256=META1, title_sha256=TITLE1))
    run_id = new_run_id()

    result = store.claim(
        msg(meta_sha256=META2, title_sha256=TITLE1, source_updated_at="2026-10-11T01:00:00.000Z"),
        run_id,
        None,
    )

    assert (result.action, result.doc_version, result.start_at) == ("METADATA", 1, StepName.INDEX)
    latest = store.latest("art_1") or {}
    assert (latest["status"], latest["stage"], latest["lease_owner"]) == (
        "CLAIMED",
        "VALIDATE",  # Index starts from here
        run_id,
    )
    assert latest["meta_sha256"] == META2
    # A retried Claim repeats it.
    assert store.claim(msg(meta_sha256=META2), run_id, None).action == "METADATA"


def test_a_new_title_is_a_new_version(store: StatusStore) -> None:
    run_to(store, msg(meta_sha256=META1, title_sha256=TITLE1))
    run_id = new_run_id()

    result = store.claim(
        msg(meta_sha256=META2, title_sha256=TITLE2, source_updated_at="2026-10-11T01:00:00.000Z"),
        run_id,
        None,
    )

    assert (result.action, result.doc_version, result.start_at) == ("RUN", 2, StepName.FETCH)
    assert store.get(keys.run_key("art_1", run_id))["claim"] == "CHANGED"  # type: ignore[index]
    assert (store.latest("art_1") or {})["title_sha256"] == TITLE2


def test_metadata_changes_of_articles_with_nothing_indexed_are_skipped(
    store: StatusStore,
) -> None:
    run_to(store, msg(meta_sha256=META1), ArticleStatus.FAILED_PERMANENT)

    result = store.claim(msg(meta_sha256=META2), new_run_id(), None)

    assert result.action == "SKIP"


def test_a_retried_skip_is_counted_once(store: StatusStore) -> None:
    run_to(store, msg())
    run_id = new_run_id()
    store.claim(msg(process_id="prc_B"), run_id, None)

    again = store.claim(msg(process_id="prc_B"), run_id, None)

    assert again.action == "SKIP"
    assert summary(store, "prc_B") == {"indexed": 0, "skipped": 1, "failed": 0}


def test_replay_from_a_step_starts_over_when_the_content_changed(store: StatusStore) -> None:
    run_to(store, msg())

    result = store.claim(msg(SHA2, from_stage="INDEX", trigger="RECONCILER"), new_run_id(), None)

    # The staged vectors are the old content's; indexing them under SHA2 would be wrong.
    assert (result.action, result.doc_version, result.start_at) == ("RUN", 2, StepName.FETCH)


def test_an_older_producer_update_is_superseded(store: StatusStore) -> None:
    run_to(store, msg(SHA2, source_updated_at="2026-10-11T10:00:00.000Z"))
    late = new_run_id()

    result = store.claim(
        msg(SHA1, process_id="prc_B", source_updated_at="2026-10-11T09:00:00.000Z"), late, None
    )

    assert result.action == "SKIP"
    assert store.get(keys.run_key("art_1", late))["outcome"] == "SUPERSEDED"  # type: ignore[index]
    latest = store.latest("art_1")
    assert (latest["content_sha256"], latest["doc_version"]) == (SHA2, 1)  # type: ignore[index]
    assert summary(store, "prc_B") == {"indexed": 0, "skipped": 1, "failed": 0}


def test_a_replay_with_the_same_update_time_still_runs(store: StatusStore) -> None:
    run_to(store, msg())

    result = store.claim(msg(force=True, trigger="MANUAL"), new_run_id(), None)

    assert result.action == "RUN"


def test_an_unchanged_newer_update_moves_the_source_time_forward(store: StatusStore) -> None:
    run_to(store, msg())

    store.claim(msg(source_updated_at="2026-10-12T00:00:00.000Z"), new_run_id(), None)

    assert store.latest("art_1")["source_updated_at"] == "2026-10-12T00:00:00.000Z"  # type: ignore[index]


def test_a_finished_run_cannot_be_marked_failed_afterwards(store: StatusStore) -> None:
    run_id = run_to(store, msg())
    ref = StepRef("art_1", run_id, "prc_A")

    with pytest.raises(IngestStale):
        store.finish(ref, ArticleStatus.FAILED_PERMANENT, RunOutcome.FAILED_PERMANENT)
    with pytest.raises(IngestStale):
        store.mark_retryable(ref, "E_TIMEOUT", "late", 0)
    assert store.latest("art_1")["status"] == "INDEXED"  # type: ignore[index]
    assert summary(store, "prc_A") == {"indexed": 1, "skipped": 0, "failed": 0}


def test_escalating_a_retryable_failure_needs_no_lease(store: StatusStore) -> None:
    run_id = new_run_id()
    store.claim(msg(), run_id, None)
    ref = StepRef("art_1", run_id, "prc_A")
    store.mark_retryable(ref, "E_TIMEOUT", "slow", 0)

    store.finish(
        ref,
        ArticleStatus.FAILED_PERMANENT,
        RunOutcome.FAILED_PERMANENT,
        require=ArticleStatus.FAILED_RETRYABLE,
    )

    assert store.latest("art_1")["status"] == "FAILED_PERMANENT"  # type: ignore[index]


def test_delete_of_unknown_article_is_done(store: StatusStore) -> None:
    remove = IngestMessage(article_id="art_x", event="REMOVE", trigger="STREAM")
    assert store.claim(remove, new_run_id(), None).action == "DONE"


REMOVE_AT = "2026-10-11T00:00:00.000Z"  # the deleted row's last update (stream OldImage)


def remove_msg(**kw: Any) -> IngestMessage:
    return IngestMessage(
        article_id="art_1", event="REMOVE", trigger="STREAM", source_updated_at=REMOVE_AT, **kw
    )


def test_a_delete_during_a_run_marks_the_article_and_is_acked(store: StatusStore) -> None:
    store.claim(msg(), new_run_id(), None)
    delete_run = new_run_id()

    result = store.claim(remove_msg(), delete_run, None)

    assert result.action == "DONE"  # acked, not dropped: the running run will hand over
    run = store.get(keys.run_key("art_1", delete_run))
    assert run["outcome"] == "DELETE_DEFERRED"  # type: ignore[index]
    latest = store.latest("art_1")
    assert latest["gsi4pk"] == keys.DELETE_PENDING  # type: ignore[index]
    assert [i["pk"] for i in store.pending_deletes()] == ["art_1"]


def test_a_run_meets_the_pending_delete_at_its_next_step(store: StatusStore) -> None:
    run_id = new_run_id()
    store.claim(msg(), run_id, None)
    ref = StepRef("art_1", run_id, "prc_A")
    store.start_step(ref, StepName.FETCH, None)
    store.claim(remove_msg(), new_run_id(), None)

    with pytest.raises(DeletePending):
        store.start_step(ref, StepName.EXTRACT, None)
    with pytest.raises(DeletePending):
        store.touch_lease(ref, StepName.FETCH)
    with pytest.raises(DeletePending):
        store.finish(ref, ArticleStatus.INDEXED, RunOutcome.INDEXED)

    store.yield_to_delete(ref)
    delete_run = new_run_id()
    claimed = store.claim(remove_msg(), delete_run, None)  # the handed-over REMOVE
    assert claimed.action == "DELETE"
    store.finish(StepRef("art_1", delete_run, None), ArticleStatus.DELETED, RunOutcome.DELETED)
    latest = store.latest("art_1")
    assert latest["status"] == "DELETED"  # type: ignore[index]
    assert "delete_requested_at" not in latest  # type: ignore[operator]
    assert store.pending_deletes() == []


def test_a_stale_update_cannot_bring_a_deleted_article_back(store: StatusStore) -> None:
    run_to(store, msg())
    delete_run = new_run_id()
    store.claim(remove_msg(), delete_run, None)
    store.finish(StepRef("art_1", delete_run, None), ArticleStatus.DELETED, RunOutcome.DELETED)

    late = store.claim(msg(process_id="prc_B"), new_run_id(), None)  # a redelivered UPSERT

    assert late.action == "SKIP"
    assert store.latest("art_1")["status"] == "DELETED"  # type: ignore[index]


def test_a_newer_upload_after_a_delete_is_a_new_version(store: StatusStore) -> None:
    run_to(store, msg())
    delete_run = new_run_id()
    store.claim(remove_msg(), delete_run, None)
    store.finish(StepRef("art_1", delete_run, None), ArticleStatus.DELETED, RunOutcome.DELETED)

    again = store.claim(msg(SHA2, source_updated_at="2026-10-12T00:00:00.000Z"), new_run_id(), None)

    assert (again.action, again.doc_version) == ("RUN", 2)


def test_a_newer_update_cancels_a_pending_delete(store: StatusStore) -> None:
    first = new_run_id()
    store.claim(msg(), first, None)
    store.claim(remove_msg(), new_run_id(), None)
    _expire_lease(store)

    newer = store.claim(msg(SHA2, source_updated_at="2026-10-12T00:00:00.000Z"), new_run_id(), None)

    assert newer.action == "RUN"
    assert "delete_requested_at" not in store.latest("art_1")  # type: ignore[operator]
    assert store.pending_deletes() == []


def test_an_orphan_delete_runs_without_a_latest(store: StatusStore) -> None:
    orphan = IngestMessage(article_id="art_x", event="REMOVE", trigger="RECONCILER", force=True)

    result = store.claim(orphan, new_run_id(), None)

    assert (result.action, result.start_at) == ("DELETE", StepName.INDEX)


def test_step_protocol_happy_path(store: StatusStore) -> None:
    run_id = new_run_id()
    store.claim(msg(), run_id, None)
    ref = StepRef("art_1", run_id, "prc_A")

    store.start_step(ref, StepName.FETCH, None)
    store.succeed_step(ref, StepName.FETCH, 12, metrics={"size": 10})
    store.start_step(ref, StepName.EXTRACT, "s3://x/in")
    store.succeed_step(ref, StepName.EXTRACT, 34, output_uri="s3://x/out", output_etag="abc")

    item = store.get(keys.step_key("art_1", run_id, StepName.EXTRACT))
    assert item is not None
    assert (item["step_status"], item["attempt"], item["output_etag"]) == ("SUCCEEDED", 1, "abc")
    assert store.latest("art_1")["status"] == "EXTRACTING"  # type: ignore[index]


def test_steps_cannot_skip_ahead_or_run_for_a_stale_run(store: StatusStore) -> None:
    run_id = new_run_id()
    store.claim(msg(), run_id, None)

    with pytest.raises(IngestStale):
        store.start_step(StepRef("art_1", run_id, "prc_A"), StepName.CHUNK, None)  # not after FETCH
    with pytest.raises(IngestStale):
        store.start_step(StepRef("art_1", new_run_id(), "prc_A"), StepName.FETCH, None)


def test_retry_updates_the_same_step_item(store: StatusStore) -> None:
    run_id = new_run_id()
    store.claim(msg(), run_id, None)
    ref = StepRef("art_1", run_id, "prc_A")

    for _ in range(7):
        store.start_step(ref, StepName.FETCH, None)
        store.fail_step(ref, StepName.FETCH, 5, IngestTransient(ErrorCode.THROTTLED, "slow down"))
    store.start_step(ref, StepName.FETCH, None)
    store.succeed_step(ref, StepName.FETCH, 5)

    item = store.get(keys.step_key("art_1", run_id, StepName.FETCH))
    assert item is not None
    assert item["attempt"] == 8
    assert item["step_status"] == "SUCCEEDED"
    assert len(item["attempts"]) == 5  # capped
    assert "gsi4pk" not in item
    assert len(store.run_items("art_1", run_id)) == 2  # RUN# + one step item, not one per attempt


def test_failed_step_is_indexed_for_stepfail_queries(store: StatusStore) -> None:
    run_id = new_run_id()
    store.claim(msg(), run_id, None)
    ref = StepRef("art_1", run_id, "prc_A")
    store.start_step(ref, StepName.FETCH, None)

    store.fail_step(ref, StepName.FETCH, 5, IngestPermanent(ErrorCode.SOURCE_MISSING, "gone"))

    item = store.get(keys.step_key("art_1", run_id, StepName.FETCH))
    assert item is not None
    assert (item["gsi4pk"], item["error_code"]) == ("STEPFAIL#FETCH", "E_SOURCE_MISSING")


def test_validate_success_marks_the_article_staged(store: StatusStore) -> None:
    run_id = new_run_id()
    store.claim(
        msg(from_stage="VALIDATE", trigger="MANUAL"), run_id, None
    )  # no LATEST yet -> full run
    ref = StepRef("art_1", run_id, "prc_A")
    for step in (
        StepName.FETCH,
        StepName.EXTRACT,
        StepName.NORMALIZE,
        StepName.CHUNK,
        StepName.EMBED,
        StepName.VALIDATE,
    ):
        store.start_step(ref, step, None)
        store.succeed_step(ref, step, 1)

    assert store.latest("art_1")["status"] == "STAGED"  # type: ignore[index]


# -- terminal states and roll-up ---------------------------------------------------


def test_finish_releases_the_lease_and_counts_once(store: StatusStore) -> None:
    run_to(store, msg())
    run_to(store, msg(SHA2))  # content changed again in the same process

    latest = store.latest("art_1")
    assert latest is not None
    assert latest["status"] == "INDEXED"
    assert "lease_owner" not in latest
    assert "gsi3pk" not in latest
    assert summary(store, "prc_A") == {"indexed": 1, "skipped": 0, "failed": 0}


def _conflicts(store: StatusStore, monkeypatch: pytest.MonkeyPatch, times: int) -> list[int]:
    """Make the next `times` transactions fail with TransactionConflict, as DynamoDB does when
    concurrent transactions touch the same item (here: the process SUMMARY)."""
    real = store.client.transact_write_items
    calls = [0]

    def flaky(**kwargs: Any) -> Any:
        calls[0] += 1
        if calls[0] <= times:
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException", "Message": "conflict"},
                    "CancellationReasons": [{"Code": "None"}, {"Code": "TransactionConflict"}],
                },
                "TransactWriteItems",
            )
        return real(**kwargs)

    monkeypatch.setattr(store.client, "transact_write_items", flaky)
    monkeypatch.setattr("ingest.status.base._CONFLICT_BASE_S", 0)
    return calls


def test_transaction_conflicts_are_retried_not_taken_as_a_lost_lease(
    store: StatusStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = new_run_id()
    store.claim(msg(), run_id, None)
    calls = _conflicts(store, monkeypatch, times=2)

    store.finish(StepRef("art_1", run_id, "prc_A"), ArticleStatus.INDEXED, RunOutcome.INDEXED)

    assert calls[0] == 3
    assert summary(store, "prc_A") == {"indexed": 1, "skipped": 0, "failed": 0}


def test_persistent_conflicts_are_transient(
    store: StatusStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = new_run_id()
    store.claim(msg(), run_id, None)
    _conflicts(store, monkeypatch, times=100)

    with pytest.raises(IngestTransient) as err:
        store.finish(StepRef("art_1", run_id, "prc_A"), ArticleStatus.INDEXED, RunOutcome.INDEXED)
    assert err.value.code == ErrorCode.DB_CONFLICT


def _cancel_with(store: StatusStore, monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    def cancelled(**kwargs: Any) -> Any:
        raise ClientError(
            {
                "Error": {"Code": "TransactionCanceledException", "Message": code},
                "CancellationReasons": [{"Code": "None"}, {"Code": code}],
            },
            "TransactWriteItems",
        )

    monkeypatch.setattr(store.client, "transact_write_items", cancelled)


@pytest.mark.parametrize(
    ("code", "error", "error_code"),
    [
        ("ThrottlingError", IngestThrottled, ErrorCode.THROTTLED),
        ("ProvisionedThroughputExceeded", IngestThrottled, ErrorCode.THROTTLED),
        ("ValidationError", IngestPermanent, ErrorCode.BUG),
        ("ItemCollectionSizeLimitExceeded", IngestPermanent, ErrorCode.BUG),
        ("InternalServerError", IngestTransient, ErrorCode.UPSTREAM_5XX),
    ],
)
def test_transaction_cancellations_are_classified_by_reason(
    store: StatusStore,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    error: type[Exception],
    error_code: ErrorCode,
) -> None:
    run_id = new_run_id()
    store.claim(msg(), run_id, None)
    _cancel_with(store, monkeypatch, code)

    with pytest.raises(error) as err:
        store.finish(StepRef("art_1", run_id, "prc_A"), ArticleStatus.INDEXED, RunOutcome.INDEXED)
    assert err.value.code == error_code  # type: ignore[attr-defined]


def _expire_lease(store: StatusStore) -> None:
    store.table.update_item(
        Key=keys.latest_key("art_1"),
        UpdateExpression="SET lease_expires_at = :past",
        ExpressionAttributeValues={":past": 1},
    )


def test_a_run_past_its_lease_goes_on_while_nobody_took_over(store: StatusStore) -> None:
    run_id = new_run_id()
    store.claim(msg(), run_id, None)
    ref = StepRef("art_1", run_id, "prc_A")
    store.start_step(ref, StepName.FETCH, None)
    _expire_lease(store)

    store.touch_lease(ref, StepName.FETCH)
    store.succeed_step(ref, StepName.FETCH, 5)
    store.start_step(ref, StepName.EXTRACT, None)

    latest = store.latest("art_1")
    assert latest is not None
    assert int(latest["lease_expires_at"]) > 1


def test_a_run_whose_article_was_taken_over_is_stale(store: StatusStore) -> None:
    first = new_run_id()
    store.claim(msg(), first, None)
    ref = StepRef("art_1", first, "prc_A")
    store.start_step(ref, StepName.FETCH, None)
    _expire_lease(store)
    second = new_run_id()
    assert store.claim(msg(SHA2), second, None).action == "RUN"  # the lease had expired

    with pytest.raises(IngestStale):
        store.touch_lease(ref, StepName.FETCH)
    with pytest.raises(IngestStale):
        store.start_step(ref, StepName.EXTRACT, None)


def test_replay_moves_a_failure_to_indexed(store: StatusStore) -> None:
    # UC5: failed permanently, fixed, replayed -> failed -1, indexed +1.
    run_to(store, msg(), ArticleStatus.FAILED_PERMANENT)
    assert summary(store, "prc_A") == {"indexed": 0, "skipped": 0, "failed": 1}

    run_to(store, msg(force=True, trigger="MANUAL"))

    assert summary(store, "prc_A") == {"indexed": 1, "skipped": 0, "failed": 0}


def test_finish_by_a_stale_run_is_refused(store: StatusStore) -> None:
    run_id = new_run_id()
    store.claim(msg(), run_id, None)

    with pytest.raises(IngestStale):
        store.finish(
            StepRef("art_1", new_run_id(), "prc_A"), ArticleStatus.INDEXED, RunOutcome.INDEXED
        )
    assert store.summary("prc_A") is None


def test_process_completes_when_counts_reach_registered(store: StatusStore) -> None:
    run_to(store, msg())
    producer_process = {
        "status": "CLOSED",
        "expected_count": 2,
        "registered_count": 2,
        "rejected_count": 0,
        "tickets_issued": 2,
        "pending_uploads": 0,
    }

    assert store.complete_process_if_done("prc_A", producer_process) is None  # 1 of 2

    other = IngestMessage.model_validate(msg().model_dump() | {"article_id": "art_2"})
    run_to(store, other, ArticleStatus.FAILED_PERMANENT)
    assert store.complete_process_if_done("prc_A", producer_process) == "COMPLETE_WITH_ERRORS"
    assert store.complete_process_if_done("prc_A", producer_process) is None  # only once


def test_unsettled_producer_process_never_completes(store: StatusStore) -> None:
    run_to(store, msg())
    open_process = {
        "status": "OPEN",
        "expected_count": None,
        "registered_count": 1,
        "rejected_count": 0,
        "tickets_issued": 1,
        "pending_uploads": 0,
    }

    assert store.complete_process_if_done("prc_A", open_process) is None
