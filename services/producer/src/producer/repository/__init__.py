"""DynamoDB access for the process, articles and producer_uploads tables.

Repository is the one object the service, registrar and janitor use; its methods come from one
class per table (processes.py, uploads.py, articles.py), which share the three tables because
several writes span two of them in one transaction.
"""

from __future__ import annotations

from producer.repository.articles import ArticleRepo
from producer.repository.keys import (
    GSI_MEMBER,
    GSI_PENDING,
    GSI_PROCESS_UPLOADS,
    GSI_SOURCE,
    MEMBER_PAGE_KEYS,
    PENDING,
    SOURCE_PAGE_KEYS,
    UPDATED_SHARDS,
    UPLOAD_PAGE_KEYS,
    Item,
    membership_id,
    sequence_id,
    tombstone_id,
    updated_shard,
)
from producer.repository.processes import ProcessRepo
from producer.repository.transactions import transact_with_retry
from producer.repository.uploads import UploadRepo

__all__ = [
    "GSI_MEMBER",
    "GSI_PENDING",
    "GSI_PROCESS_UPLOADS",
    "GSI_SOURCE",
    "MEMBER_PAGE_KEYS",
    "PENDING",
    "SOURCE_PAGE_KEYS",
    "UPDATED_SHARDS",
    "UPLOAD_PAGE_KEYS",
    "ArticleRepo",
    "Item",
    "ProcessRepo",
    "Repository",
    "UploadRepo",
    "membership_id",
    "sequence_id",
    "tombstone_id",
    "transact_with_retry",
    "updated_shard",
]


class Repository(ProcessRepo, UploadRepo, ArticleRepo):
    """Every producer table operation; Repository.from_names(process, articles, uploads)."""
