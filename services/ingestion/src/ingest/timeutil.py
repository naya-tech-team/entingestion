"""Timestamps the shared entcommon.timeutil doesn't cover: epoch seconds (DynamoDB TTLs and
lease expiries) and the date forms of producer metadata (search filters)."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from entcommon.timeutil import iso, parse_iso


def now_epoch() -> int:
    return int(time.time())


def days(n: int) -> int:
    return n * 86400


def iso_at(epoch: int) -> str:
    """Epoch seconds as ISO-8601 UTC with milliseconds (the stored format)."""
    return iso(datetime.fromtimestamp(epoch, UTC))


def iso_date(value: object) -> str | None:
    """ISO-8601 UTC to the second for common date inputs; None if unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = parse_iso(value)
    except ValueError:
        return None
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_day(value: object) -> str | None:
    """YYYY-MM-DD (UTC) for the same inputs as iso_date: the published_at filter column."""
    parsed = iso_date(value)
    return parsed[:10] if parsed else None
