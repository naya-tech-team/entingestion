"""UTC timestamps in the formats stored in DynamoDB."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(ts: datetime) -> str:
    """ISO-8601 UTC with millisecond precision; sorts lexicographically (GSI sort keys)."""
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def date_of(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%d")
