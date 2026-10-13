"""Index names, synthetic row ids and page-token key shapes of the producer tables."""

from __future__ import annotations

import zlib
from typing import Any

Item = dict[str, Any]

GSI_SOURCE = "gsi_source"  # process table: a source's processes by created_at
GSI_MEMBER = "gsi_member"  # uploads table: membership markers by process, article order
GSI_PENDING = "gsi_pending"  # uploads table: tickets without an outcome, by ticket expiry
GSI_PROCESS_UPLOADS = "gsi_process_uploads"  # uploads table: a process's uploads by created_at
PENDING = "P"  # value of the sparse `pending` attribute while an upload has no outcome
UPDATED_SHARDS = 16  # gsi_updated_shard partitions per day

# The LastEvaluatedKey of each listing query (table key plus index key): the only shapes a
# next_token may decode to.
SOURCE_PAGE_KEYS = frozenset({"process_id", "source", "created_at"})
UPLOAD_PAGE_KEYS = frozenset({"upload_id", "process_id", "created_at"})
MEMBER_PAGE_KEYS = frozenset({"upload_id", "member_process_id", "member_article_id"})


def updated_shard(article_id: str, date: str) -> str:
    """Spread a day's article writes over UPDATED_SHARDS partitions of gsi_updated_shard."""
    return f"{date}#{zlib.crc32(article_id.encode()) % UPDATED_SHARDS:02d}"


def tombstone_id(article_id: str) -> str:
    """The uploads-table row that remembers a deleted article's last upload."""
    return f"TOMBSTONE#{article_id}"


def sequence_id(article_id: str) -> str:
    """The uploads-table row holding an article's ticket counter."""
    return f"SEQ#{article_id}"


def membership_id(process_id: str, article_id: str) -> str:
    """The uploads-table row that counts an article in a process once."""
    return f"MEMBER#{process_id}#{article_id}"
