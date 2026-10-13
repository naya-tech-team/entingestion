"""DynamoDB helpers shared by the services. Transaction retry policies stay in each service."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError


def plain(value: Any) -> Any:
    """DynamoDB Decimals back to int/float, recursively through mappings and lists."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def is_condition_failure(err: ClientError) -> bool:
    """Whether a single-item write failed its ConditionExpression."""
    return err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def cancellation_codes(err: ClientError) -> list[str] | None:
    """Per-item codes of a cancelled transaction ("None" for items that didn't fail), or None if
    err isn't a TransactionCanceledException."""
    if err.response.get("Error", {}).get("Code") != "TransactionCanceledException":
        return None
    reasons = err.response.get("CancellationReasons") or []
    return [str(r.get("Code", "None")) for r in reasons]
