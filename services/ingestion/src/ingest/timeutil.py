"""UTC timestamps in the formats stored in DynamoDB."""

from __future__ import annotations

import time
from datetime import UTC, datetime


def now_epoch() -> int:
    return int(time.time())


def iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def days(n: int) -> int:
    return n * 86400


def iso_at(epoch: int) -> str:
    return (
        datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def iso_date(value: object) -> str | None:
    """ISO-8601 UTC for common date inputs; None if unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    for parse in (datetime.fromisoformat,):
        try:
            parsed = parse(raw)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return None


def iso_day(value: object) -> str | None:
    """YYYY-MM-DD (UTC) for the same inputs as iso_date: the published_at filter column."""
    parsed = iso_date(value)
    return parsed[:10] if parsed else None
