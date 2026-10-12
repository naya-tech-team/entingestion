"""Outbound notifications: EventBridge events (article.indexed, process.ingested) and SNS alerts."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import boto3

from ingest.config import get_settings

if TYPE_CHECKING:
    from mypy_boto3_events.client import EventBridgeClient
    from mypy_boto3_sns.client import SNSClient

SOURCE = "entingestion.ingestion"


@lru_cache(maxsize=1)
def _events() -> EventBridgeClient:
    return boto3.client("events")


@lru_cache(maxsize=1)
def _sns() -> SNSClient:
    return boto3.client("sns")


def emit(detail_type: str, detail: dict[str, Any]) -> None:
    _events().put_events(
        Entries=[
            {
                "Source": SOURCE,
                "DetailType": detail_type,
                "Detail": json.dumps(detail, default=str),
                "EventBusName": get_settings().event_bus_name,
            }
        ]
    )


def alert(subject: str, detail: dict[str, Any]) -> None:
    topic = get_settings().alert_topic_arn
    if topic:
        _sns().publish(
            TopicArn=topic, Subject=subject[:100], Message=json.dumps(detail, indent=2, default=str)
        )


def reset() -> None:
    _events.cache_clear()
    _sns.cache_clear()
