"""The three tables every part of the repository works on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self, cast

import boto3
from entcommon.dynamo import plain

from producer.repository.keys import Item

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table


class Tables:
    def __init__(self, process_table: Table, articles_table: Table, uploads_table: Table) -> None:
        self.process = process_table
        self.articles = articles_table
        self.uploads = uploads_table
        # The resource's client: transactions take plain Python values and it serializes them
        # itself (pre-serialized {"S": ...} values would be double-wrapped).
        self.client = process_table.meta.client

    @classmethod
    def from_names(cls, process: str, articles: str, uploads: str) -> Self:
        ddb = boto3.resource("dynamodb")
        return cls(ddb.Table(process), ddb.Table(articles), ddb.Table(uploads))


def as_item(value: Any) -> Item:
    """A stored item with DynamoDB's Decimals as plain numbers, so Pydantic sees int/float."""
    return cast(Item, plain(value))
