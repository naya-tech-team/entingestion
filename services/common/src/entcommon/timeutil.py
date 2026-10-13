"""UTC timestamps in the format the services store: ISO-8601 with milliseconds and a "Z" suffix,
so they sort lexicographically (DynamoDB sort keys)."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(ts: datetime) -> str:
    """ts as ISO-8601 UTC with millisecond precision, e.g. 2026-10-12T08:30:00.123Z."""
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_now() -> str:
    return iso(utcnow())


def parse_iso(value: str) -> datetime:
    """An aware UTC datetime from ISO-8601 ("Z" or an offset; naive means UTC).
    Raises ValueError if value isn't ISO-8601."""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
