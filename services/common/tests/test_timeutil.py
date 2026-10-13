"""UTC ISO timestamps."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

import pytest

from entcommon.timeutil import iso, iso_now, parse_iso, utcnow


def test_utcnow_is_aware_utc() -> None:
    now = utcnow()
    assert now.tzinfo is UTC


def test_iso_has_milliseconds_and_z() -> None:
    ts = datetime(2026, 10, 12, 8, 30, 0, 123456, tzinfo=UTC)
    assert iso(ts) == "2026-10-12T08:30:00.123Z"
    assert iso(ts.replace(microsecond=0)) == "2026-10-12T08:30:00.000Z"
    plus2 = timezone(timedelta(hours=2))
    assert iso(datetime(2026, 10, 12, 10, 30, tzinfo=plus2)) == "2026-10-12T08:30:00.000Z"


def test_iso_now_format() -> None:
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", iso_now())


def test_iso_sorts_like_time() -> None:
    a = datetime(2026, 1, 2, 3, 4, 5, 6000, tzinfo=UTC)
    b = a + timedelta(milliseconds=1)
    assert iso(a) < iso(b)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-12T08:30:00.123Z", datetime(2026, 10, 12, 8, 30, 0, 123000, tzinfo=UTC)),
        ("2026-10-12T10:30:00+02:00", datetime(2026, 10, 12, 8, 30, tzinfo=UTC)),
        ("2026-10-12T08:30:00", datetime(2026, 10, 12, 8, 30, tzinfo=UTC)),  # naive: UTC
        (" 2026-10-12 ", datetime(2026, 10, 12, tzinfo=UTC)),
    ],
)
def test_parse_iso(value: str, expected: datetime) -> None:
    parsed = parse_iso(value)
    assert parsed == expected
    assert parsed.tzinfo is UTC


def test_round_trip() -> None:
    ts = datetime(2026, 10, 12, 8, 30, 0, 123000, tzinfo=UTC)
    assert parse_iso(iso(ts)) == ts


@pytest.mark.parametrize("value", ["", "yesterday", "2026-13-01"])
def test_parse_iso_rejects_garbage(value: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        parse_iso(value)
