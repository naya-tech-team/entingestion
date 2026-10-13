"""Reads and writes on ingestion_status: the facade the rest of the pipeline imports.

Every write that moves an article between states is a DynamoDB transaction with a condition on
LATEST.run_id, so a run that lost its lease can't overwrite the run that owns the article
(IngestStale). Transactions go through the resource's client, which serializes plain Python
values itself (pre-serialized {"S": ...} values would be double-wrapped).

The store is assembled from one part per concern, each building on the previous:

    base         table access, paged queries, transactions (ingest.status.base)
    reads        LATEST, runs, items by status, configuration
    slots        admission slots
    rollup       the process roll-up and completion
    claim        Claim's decisions, pending deletes
    steps        the step protocol and terminal states

Pure decision rules are in ingest.status.claim_rules.
"""

from __future__ import annotations

from entcommon.dynamo import plain

from ingest.status.claim_rules import _settled
from ingest.status.records import MAX_ATTEMPTS_KEPT, ClaimResult, Item, StepRef
from ingest.status.steps import StepsMixin

__all__ = [
    "MAX_ATTEMPTS_KEPT",
    "ClaimResult",
    "Item",
    "StatusStore",
    "StepRef",
    "_settled",
    "plain",
]


class StatusStore(StepsMixin):
    """Every read and write the pipeline makes on ingestion_status."""
