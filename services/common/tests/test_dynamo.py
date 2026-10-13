"""Decimal conversion and DynamoDB error inspection."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from botocore.exceptions import ClientError

from entcommon.dynamo import cancellation_codes, is_condition_failure, plain


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal(5), 5),
        (Decimal("5.0"), 5),
        (Decimal("1.25"), 1.25),
        (Decimal(-3), -3),
        ("text", "text"),
        (None, None),
        (True, True),
        (
            {"a": Decimal(1), "b": [Decimal("0.5"), {"c": Decimal(2)}]},
            {"a": 1, "b": [0.5, {"c": 2}]},
        ),
        ([], []),
    ],
)
def test_plain(value: Any, expected: Any) -> None:
    out = plain(value)
    assert out == expected
    assert type(out) is type(expected)


def test_plain_keeps_sets_and_bytes() -> None:
    assert plain({"s": {"x"}, "b": b"x"}) == {"s": {"x"}, "b": b"x"}


def _error(code: str, reasons: list[dict[str, Any]] | None = None) -> ClientError:
    response: dict[str, Any] = {"Error": {"Code": code, "Message": "m"}}
    if reasons is not None:
        response["CancellationReasons"] = reasons
    return ClientError(response, "TransactWriteItems")  # type: ignore[arg-type]


def test_condition_failure() -> None:
    assert is_condition_failure(_error("ConditionalCheckFailedException"))
    assert not is_condition_failure(_error("TransactionCanceledException", []))
    assert not is_condition_failure(ClientError({}, "PutItem"))


def test_cancellation_codes() -> None:
    err = _error(
        "TransactionCanceledException",
        [{"Code": "None"}, {"Code": "ConditionalCheckFailed", "Message": "x"}, {}],
    )
    assert cancellation_codes(err) == ["None", "ConditionalCheckFailed", "None"]
    assert cancellation_codes(_error("TransactionCanceledException")) == []
    assert cancellation_codes(_error("ConditionalCheckFailedException")) is None
    assert cancellation_codes(_error("ThrottlingException")) is None
