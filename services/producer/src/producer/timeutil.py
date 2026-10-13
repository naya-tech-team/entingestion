"""UTC timestamps in the formats stored in DynamoDB (entcommon.timeutil, plus the day)."""

from __future__ import annotations

from datetime import UTC, datetime

from entcommon.timeutil import iso, utcnow

__all__ = ["date_of", "iso", "utcnow"]


def date_of(ts: datetime) -> str:
    """The UTC day of ts, YYYY-MM-DD (an article's updated_date)."""
    return ts.astimezone(UTC).strftime("%Y-%m-%d")
