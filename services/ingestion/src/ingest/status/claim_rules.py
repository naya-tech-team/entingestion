"""Pure predicates behind Claim's decisions and process completion (no DynamoDB access)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ingest.models.messages import IngestMessage
from ingest.models.status import (
    ArticleStatus,
)
from ingest.status.records import Item

if TYPE_CHECKING:
    pass


# An unchanged article that already reached one of these is skipped by Claim.
_SETTLED_STATUSES = frozenset(
    {ArticleStatus.INDEXED, ArticleStatus.SKIPPED, ArticleStatus.FAILED_PERMANENT}
)


def _older_than_latest(msg: IngestMessage, latest: Item) -> bool:
    """The message carries an older producer update than LATEST already has, or one no newer
    than the delete that ended the article. Producer timestamps are fixed-width ISO-8601 UTC,
    so they compare as strings."""
    mine, theirs = msg.source_updated_at, latest.get("source_updated_at")
    if not mine or not theirs:
        return False
    if latest.get("status") == ArticleStatus.DELETED or "delete_requested_at" in latest:
        return bool(mine <= theirs)
    return bool(mine < theirs)


def _title_changed(msg: IngestMessage, latest: Item) -> bool:
    """A new title is a content change: it's part of every chunk's embed_text. Unknown (an
    article indexed before titles were fingerprinted) counts as unchanged."""
    theirs = latest.get("title_sha256")
    return msg.title_sha256 is not None and theirs is not None and theirs != msg.title_sha256


def _settled(process: Item) -> bool:
    """The producer's rule (producer.models.is_settled): CLOSED, no upload ticket still pending,
    and at least expected_count tickets issued."""
    expected = process.get("expected_count")
    if process.get("status") != "CLOSED" or expected is None:
        return False
    pending, issued = int(process.get("pending_uploads", 0)), int(process.get("tickets_issued", 0))
    return pending <= 0 and issued >= int(expected)
