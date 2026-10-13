"""Admission slots (dispatcher backpressure): #SLOTS/<lane>#<i>, held by a run until it ends or
its lease runs out."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, cast

from botocore.exceptions import ClientError
from entcommon.dynamo import is_condition_failure, plain
from entcommon.timeutil import iso_now

from ingest.status import keys
from ingest.status.reads import ReadsMixin
from ingest.status.records import Item, StepRef
from ingest.timeutil import now_epoch

if TYPE_CHECKING:
    pass


class SlotsMixin(ReadsMixin):
    """Acquire, extend and release admission slots."""

    def acquire_slot(self, lane: str, size: int, run_id: str) -> str | None:
        """Take a free or expired slot of the lane for run_id; None if all `size` are held.

        A slot is held until the run ends (release_slot) or its lease runs out: runs extend it
        at every step, so only a run that died without ending (or one the state machine ended
        quietly as stale) leaves a slot behind, and only for one lease."""
        now = now_epoch()
        resp = self.table.query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :lane)",
            ExpressionAttributeValues={":pk": keys.SLOTS, ":lane": f"{lane}#"},
            ConsistentRead=True,
        )
        items = [cast(Item, plain(i)) for i in resp.get("Items", [])]
        held = {str(i["sk"]): int(i.get("expires_at", 0)) for i in items if "run_id" in i}
        free = [keys.slot_sk(lane, i) for i in range(size)]
        free = [s for s in free if held.get(s, 0) <= now]
        random.shuffle(free)  # spread contending dispatchers over the free slots
        for slot in free:
            try:
                self.table.put_item(
                    Item={
                        **keys.slot_key(slot),
                        "lane": lane,
                        "run_id": run_id,
                        "expires_at": now + self.settings.lease_seconds,
                        "acquired_at": iso_now(),
                    },
                    ConditionExpression="attribute_not_exists(run_id) OR expires_at <= :now",
                    ExpressionAttributeValues={":now": now},
                )
            except ClientError as err:
                if is_condition_failure(err):
                    continue  # another dispatcher took it first
                raise
            return slot
        return None

    def extend_slot(self, ref: StepRef) -> None:
        """Keep the run's slot while it works (every step start and heartbeat). Best effort: a
        slot that expired and went to another run stays with that run."""
        if not ref.slot:
            return
        try:
            self.table.update_item(
                Key=keys.slot_key(ref.slot),
                UpdateExpression="SET expires_at = :exp",
                ConditionExpression="run_id = :run",
                ExpressionAttributeValues={
                    ":exp": now_epoch() + self.settings.lease_seconds,
                    ":run": ref.run_id,
                },
            )
        except ClientError:
            return  # taken over, or DynamoDB refused: the slot's lease decides

    def release_slot(self, slot: str | None, run_id: str) -> None:
        """Free the slot if run_id still holds it. Idempotent, and never fails the run: a slot
        that can't be released now frees itself when its lease runs out."""
        if not slot:
            return
        try:
            self.table.update_item(
                Key=keys.slot_key(slot),
                UpdateExpression="SET released_at = :ts REMOVE run_id, expires_at",
                ConditionExpression="run_id = :run",
                ExpressionAttributeValues={":ts": iso_now(), ":run": run_id},
            )
        except ClientError:
            return  # already free or taken over (or DynamoDB refused: the lease frees it)

    def _release(self, ref: StepRef) -> None:
        self.release_slot(ref.slot, ref.run_id)
