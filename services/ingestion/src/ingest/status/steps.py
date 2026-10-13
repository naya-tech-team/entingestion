"""The step write protocol on ingestion_status (start, heartbeat, succeed, fail) and the terminal
states (finish, retryable), each one conditional on the run still holding the article."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from botocore.exceptions import ClientError
from entcommon.dynamo import is_condition_failure
from entcommon.timeutil import iso_now

from ingest.errors import DeletePending, IngestError, IngestStale
from ingest.models.status import (
    ROLLUP_COUNTER,
    STEP_STATUS,
    STEP_SUCCESS_STATUS,
    TERMINAL_STATUSES,
    ArticleStatus,
    RunOutcome,
    StepName,
    StepStatus,
)
from ingest.status import keys
from ingest.status.claim import ClaimMixin
from ingest.status.records import MAX_ATTEMPTS_KEPT, StepRef
from ingest.timeutil import now_epoch

if TYPE_CHECKING:
    pass


class StepsMixin(ClaimMixin):
    """Steps and terminal states of a run."""

    def _finish_shadow(self, ref: StepRef, status: ArticleStatus, outcome: RunOutcome) -> None:
        """End a shadow run: its SHADOW item and RUN#, and on success LATEST's version marker
        for the table. LATEST's status and the process roll-up are the live run's."""
        if ref.lane is None:
            raise ValueError("not a shadow run")
        ts = iso_now()
        codes = self._transact(
            [
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": self._head_key(ref),
                        "UpdateExpression": (
                            "SET #status = :st, updated_at = :ts REMOVE lease_owner,"
                            " lease_expires_at"
                        ),
                        "ConditionExpression": "run_id = :run AND lease_owner = :run",
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            ":st": status.value,
                            ":ts": ts,
                            ":run": ref.run_id,
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": keys.run_key(ref.article_id, ref.run_id),
                        "UpdateExpression": "SET #outcome = :o, ended_at = :ts",
                        "ExpressionAttributeNames": {"#outcome": "outcome"},
                        "ExpressionAttributeValues": {":o": outcome.value, ":ts": ts},
                    }
                },
            ]
        )
        if codes is not None:
            raise IngestStale(f"{ref.article_id}: shadow run {ref.run_id} lost its lease")
        self._release(ref)
        if status is ArticleStatus.INDEXED and ref.doc_version is not None:
            self.record_table_version(ref.article_id, ref.lane, ref.doc_version)

    def _lost(self, ref: StepRef, what: str) -> IngestStale:
        """Why a run's conditional write failed: a pending delete it must hand over, or another
        run owns the article. A shadow run never hands over; the live run deals with deletes."""
        if ref.lane:
            return IngestStale(f"{ref.article_id}: shadow run {ref.run_id} stopped {what}")
        latest = self.latest(ref.article_id) or {}
        if (
            latest.get("run_id") == ref.run_id
            and latest.get("lease_owner") == ref.run_id
            and "delete_requested_at" in latest
        ):
            return DeletePending(f"{ref.article_id}: deleted while run {ref.run_id} {what}")
        return IngestStale(f"{ref.article_id}: run {ref.run_id} lost its lease {what}")

    def start_step(
        self,
        ref: StepRef,
        step: StepName,
        input_uri: str | None,
        *,
        count_attempt: bool = True,
    ) -> None:
        """Step 1: move LATEST to this step and mark the step item STARTED (attempt + 1).

        count_attempt=False reopens a step that another state continues (Extract -> OCR) without
        counting it as a retry.

        Raises IngestStale if this run no longer owns the article.
        """
        now, ts = now_epoch(), iso_now()
        status = STEP_STATUS[step]
        expires = now + self.settings.lease_seconds
        if ref.lane:
            sets = [
                "#status = :st",
                "updated_at = :ts",
                "#stage = :step",
                "lease_expires_at = :exp",
            ]
            values: dict[str, Any] = {":st": status.value, ":ts": ts}
        else:
            sets, values = self._status_sets(status, ref.article_id, ts)
            sets += ["#stage = :step", "lease_expires_at = :exp", "gsi3sk = :exp"]
        previous = step.previous or StepName.CLAIM
        values |= {
            ":step": step.value,
            ":prev": previous.value,
            ":exp": expires,
            ":run": ref.run_id,
        }
        latest_update = {
            "Update": {
                "TableName": self.table.name,
                "Key": self._head_key(ref),
                "UpdateExpression": "SET " + ", ".join(sets),
                # The lease owner, not the clock, decides: a run past its lease may go on until
                # another run takes the article over (which replaces lease_owner).
                "ConditionExpression": (
                    "run_id = :run AND lease_owner = :run AND #stage IN (:prev, :step)"
                    " AND attribute_not_exists(delete_requested_at)"
                ),
                "ExpressionAttributeNames": {"#status": "status", "#stage": "stage"},
                "ExpressionAttributeValues": values,
            }
        }
        step_values: dict[str, Any] = {
            ":started": StepStatus.STARTED.value,
            ":ts": ts,
            ":run": ref.run_id,
            ":aid": ref.article_id,
            ":name": step.value,
            ":one": 1,
            ":exp": self._step_ttl(now),
        }
        step_sets = [
            "step_status = :started",
            "started_at = :ts",
            "run_id = :run",
            "article_id = :aid",
            "#step = :name",
            "expires_at_epoch = :exp",
        ]
        if input_uri:
            step_sets.append("input_uri = :in")
            step_values[":in"] = input_uri
        step_update = {
            "Update": {
                "TableName": self.table.name,
                "Key": keys.step_key(ref.article_id, ref.run_id, step),
                "UpdateExpression": "SET "
                + ", ".join(step_sets)
                + (" ADD #attempt :one" if count_attempt else "")
                + " REMOVE ended_at, error_class, error_code, error_msg, gsi4pk, gsi4sk",
                "ExpressionAttributeNames": {"#step": "step"}
                | ({"#attempt": "attempt"} if count_attempt else {}),
                "ExpressionAttributeValues": step_values
                if count_attempt
                else {k: v for k, v in step_values.items() if k != ":one"},
            }
        }
        items = [latest_update, step_update]
        if ref.lane and ref.doc_version is not None:
            items.append(self._latest_is_version(ref.article_id, ref.doc_version))
        codes = self._transact(items)
        if codes is not None:
            raise self._lost(ref, f"before {step}")
        self.extend_slot(ref)

    def annotate(self, ref: StepRef, attrs: Mapping[str, Any]) -> None:
        """Record facts about the run's version on LATEST (e.g. its PII finding), only while this
        run holds the article. Raises IngestStale otherwise."""
        names = {f"#a{i}": k for i, k in enumerate(attrs)}
        values: dict[str, Any] = {f":a{i}": v for i, v in enumerate(attrs.values())}
        try:
            self.table.update_item(
                Key=self._head_key(ref),
                UpdateExpression="SET " + ", ".join(f"#a{i} = :a{i}" for i in range(len(attrs))),
                ConditionExpression="run_id = :run AND lease_owner = :run",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values | {":run": ref.run_id},
            )
        except ClientError as err:
            if is_condition_failure(err):
                raise IngestStale(f"{ref.article_id}: run {ref.run_id} lost its lease") from err
            raise

    def touch_lease(self, ref: StepRef, step: StepName) -> None:
        """Extend the lease of a run that is still in `step` (long waits such as OCR).

        Raises IngestStale if the run no longer owns the article or moved on.
        """
        now = now_epoch()
        expires = now + self.settings.lease_seconds
        try:
            self.table.update_item(
                Key=self._head_key(ref),
                UpdateExpression="SET lease_expires_at = :exp, updated_at = :ts"
                + ("" if ref.lane else ", gsi3sk = :exp"),
                # A pending delete stops a long step at its next heartbeat.
                ConditionExpression=(
                    "run_id = :run AND lease_owner = :run AND #stage = :step"
                    " AND attribute_not_exists(delete_requested_at)"
                ),
                ExpressionAttributeNames={"#stage": "stage"},
                ExpressionAttributeValues={
                    ":exp": expires,
                    ":ts": iso_now(),
                    ":run": ref.run_id,
                    ":step": step.value,
                },
            )
        except ClientError as err:
            if is_condition_failure(err):
                raise self._lost(ref, f"during {step}") from err
            raise
        self.extend_slot(ref)

    def succeed_step(
        self,
        ref: StepRef,
        step: StepName,
        duration_ms: int,
        *,
        output_uri: str | None = None,
        output_etag: str | None = None,
        metrics: Mapping[str, Any] | None = None,
        terminal: bool = False,
    ) -> None:
        """Step 3: record success and extend the lease (unless the step ended the run)."""
        now, ts = now_epoch(), iso_now()
        values: dict[str, Any] = {
            ":ok": StepStatus.SUCCEEDED.value,
            ":ts": ts,
            ":ms": duration_ms,
            ":metrics": dict(metrics or {}),
        }
        sets = ["step_status = :ok", "ended_at = :ts", "duration_ms = :ms", "#metrics = :metrics"]
        if output_uri:
            sets.append("output_uri = :out")
            values[":out"] = output_uri
        if output_etag:
            sets.append("output_etag = :etag")
            values[":etag"] = output_etag
        items: list[dict[str, Any]] = [
            {
                "Update": {
                    "TableName": self.table.name,
                    "Key": keys.step_key(ref.article_id, ref.run_id, step),
                    "UpdateExpression": "SET "
                    + ", ".join(sets)
                    + " REMOVE gsi4pk, gsi4sk, error_class, error_code, error_msg",
                    "ExpressionAttributeNames": {"#metrics": "metrics"},
                    "ExpressionAttributeValues": values,
                }
            }
        ]
        if not terminal:
            expires = now + self.settings.lease_seconds
            latest_sets = ["lease_expires_at = :exp"] + ([] if ref.lane else ["gsi3sk = :exp"])
            latest_values: dict[str, Any] = {
                ":exp": expires,
                ":run": ref.run_id,
                ":step": step.value,
            }
            names = {"#stage": "stage"}
            done_status = STEP_SUCCESS_STATUS.get(step)
            if done_status is not None and ref.lane:
                latest_sets.append("#status = :st")
                latest_values[":st"] = done_status.value
                names["#status"] = "status"
            elif done_status is not None:
                status_sets, status_values = self._status_sets(done_status, ref.article_id, ts)
                latest_sets += status_sets
                latest_values |= status_values
                names["#status"] = "status"
            items.append(
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": self._head_key(ref),
                        "UpdateExpression": "SET " + ", ".join(latest_sets),
                        "ConditionExpression": "run_id = :run AND #stage = :step",
                        "ExpressionAttributeNames": names,
                        "ExpressionAttributeValues": latest_values,
                    }
                }
            )
        if self._transact(items) is not None:
            raise IngestStale(f"{ref.article_id}: run {ref.run_id} lost its lease during {step}")

    def fail_step(self, ref: StepRef, step: StepName, duration_ms: int, err: IngestError) -> None:
        """Step 4: mark the step FAILED (last 5 attempts kept) and index it for gsi4-stepfail."""
        ts = iso_now()
        key = keys.step_key(ref.article_id, ref.run_id, step)
        current = self.get(key) or {}
        attempts = list(current.get("attempts") or [])
        attempts.append(
            {"attempt": int(current.get("attempt", 1)), "error_code": err.code, "at": ts}
        )
        self.table.update_item(
            Key=key,
            UpdateExpression=(
                "SET step_status = :failed, ended_at = :ts, duration_ms = :ms, error_class = :cls,"
                " error_code = :code, error_msg = :msg, #attempts = :attempts,"
                " gsi4pk = :g4pk, gsi4sk = :ts"
            ),
            ExpressionAttributeNames={"#attempts": "attempts"},
            ExpressionAttributeValues={
                ":failed": StepStatus.FAILED.value,
                ":ts": ts,
                ":ms": duration_ms,
                ":cls": err.error_class.value,
                ":code": err.code,
                ":msg": err.detail,
                ":attempts": attempts[-MAX_ATTEMPTS_KEPT:],
                ":g4pk": keys.stepfail_pk(step),
            },
        )

    def fail_running_step(self, ref: StepRef, code: str, detail: str) -> None:
        """For failures the step couldn't record itself (Lambda timeout, crash): mark the step
        that LATEST says is running as FAILED, if it's still STARTED."""
        latest = self.latest(ref.article_id)
        if not latest or latest.get("run_id") != ref.run_id:
            return
        try:
            step = StepName(str(latest.get("stage")))
        except ValueError:
            return
        if step is StepName.CLAIM:
            return
        ts = iso_now()
        try:
            self.table.update_item(
                Key=keys.step_key(ref.article_id, ref.run_id, step),
                UpdateExpression=(
                    "SET step_status = :failed, ended_at = :ts, error_class = :cls,"
                    " error_code = :code, error_msg = :msg, gsi4pk = :g4pk, gsi4sk = :ts"
                ),
                ConditionExpression="step_status = :started",
                ExpressionAttributeValues={
                    ":failed": StepStatus.FAILED.value,
                    ":started": StepStatus.STARTED.value,
                    ":ts": ts,
                    ":cls": "permanent",
                    ":code": code,
                    ":msg": detail[:1024],
                    ":g4pk": keys.stepfail_pk(step),
                },
            )
        except ClientError as err:
            if not is_condition_failure(err):
                raise

    def finish(
        self,
        ref: StepRef,
        status: ArticleStatus,
        outcome: RunOutcome,
        latest_sets: Mapping[str, Any] | None = None,
        *,
        require: ArticleStatus | None = None,
        counter: str | None = None,
    ) -> None:
        """Move LATEST into a terminal status, end the run, release the lease and update the
        process roll-up, all in one transaction.

        The run must still hold the lease, so a late failure can't overwrite a finished run
        (MarkFailed after Finalize). require=<status> instead ends a run that holds no lease
        (the reconciler escalating a FAILED_RETRYABLE article).

        counter: count the run for its process as this instead of the status's counter, and
        only if the article has no outcome there yet (a METADATA run: INDEXED again, counted
        like an unchanged article)."""
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"{status} is not terminal")
        if ref.lane:
            self._finish_shadow(ref, status, outcome)
            return
        ts = iso_now()
        sets, values = self._status_sets(status, ref.article_id, ts)
        names = {"#status": "status"}
        for i, (name, value) in enumerate(sorted((latest_sets or {}).items())):
            names[f"#x{i}"] = name
            values[f":x{i}"] = value
            sets.append(f"#x{i} = :x{i}")
        values[":run"] = ref.run_id
        if require is None:
            condition = "run_id = :run AND lease_owner = :run"
        else:
            condition = "run_id = :run AND #status = :required"
            values[":required"] = require.value
        removes = "lease_owner, lease_expires_at, gsi3pk, gsi3sk"
        if status is ArticleStatus.DELETED:
            removes += ", delete_requested_at, delete_run_id, gsi4pk, gsi4sk"
        else:
            condition += " AND attribute_not_exists(delete_requested_at)"
        if status is not ArticleStatus.FAILED_PERMANENT:
            removes += (
                ", error_class, error_code, error_msg, next_retry_at, retry_count, throttle_count"
            )
        latest_update = {
            "Update": {
                "TableName": self.table.name,
                "Key": keys.latest_key(ref.article_id),
                "UpdateExpression": "SET " + ", ".join(sets) + " REMOVE " + removes,
                "ConditionExpression": condition,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": values,
            }
        }
        run_update = {
            "Update": {
                "TableName": self.table.name,
                "Key": keys.run_key(ref.article_id, ref.run_id),
                "UpdateExpression": "SET #outcome = :outcome, ended_at = :ts",
                "ExpressionAttributeNames": {"#outcome": "outcome"},
                "ExpressionAttributeValues": {":outcome": outcome.value, ":ts": ts},
            }
        }
        try:
            self._with_rollup(
                [latest_update, run_update],
                process_id=ref.process_id,
                article_id=ref.article_id,
                counter=counter or ROLLUP_COUNTER.get(status),
                run_id=ref.run_id,
                ts=ts,
                latest_index=0,
                first_only=counter is not None,
            )
        except IngestStale:
            raise self._lost(ref, f"before finishing {status}") from None
        self._release(ref)

    def mark_retryable(
        self,
        ref: StepRef,
        code: str,
        detail: str,
        next_retry_at: int,
        *,
        counter: Literal["retry_count", "throttle_count"] = "retry_count",
    ) -> None:
        """Transient failure after the state machine's retries: the reconciler re-queues later.
        (A shadow run just ends failed; the drift check re-queues what's still missing.)

        counter: which budget the failure uses up. Throttling (throttle_count) has its own, so a
        busy account doesn't exhaust the retries meant for real failures (retry_count)."""
        if ref.lane:
            self._finish_shadow(ref, ArticleStatus.FAILED_RETRYABLE, RunOutcome.FAILED_RETRYABLE)
            return
        ts = iso_now()
        sets, values = self._status_sets(ArticleStatus.FAILED_RETRYABLE, ref.article_id, ts)
        sets += [
            "error_class = :cls",
            "error_code = :code",
            "error_msg = :msg",
            "next_retry_at = :next",
        ]
        values |= {
            ":cls": "transient",
            ":code": code,
            ":msg": detail[:1024],
            ":next": next_retry_at,
            ":run": ref.run_id,
            ":one": 1,
        }
        codes = self._transact(
            [
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": keys.latest_key(ref.article_id),
                        "UpdateExpression": "SET "
                        + ", ".join(sets)
                        + " ADD #counter :one REMOVE lease_owner, lease_expires_at, gsi3pk,"
                        " gsi3sk",
                        "ConditionExpression": (
                            "run_id = :run AND lease_owner = :run"
                            " AND attribute_not_exists(delete_requested_at)"
                        ),
                        "ExpressionAttributeNames": {"#status": "status", "#counter": counter},
                        "ExpressionAttributeValues": values,
                    }
                },
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": keys.run_key(ref.article_id, ref.run_id),
                        "UpdateExpression": "SET #outcome = :o, ended_at = :ts",
                        "ExpressionAttributeNames": {"#outcome": "outcome"},
                        "ExpressionAttributeValues": {
                            ":o": RunOutcome.FAILED_RETRYABLE.value,
                            ":ts": ts,
                        },
                    }
                },
            ]
        )
        if codes is not None:
            raise self._lost(ref, "before marking it retryable")
        self._release(ref)
