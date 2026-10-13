"""next_token round trips, tampering and bindings."""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from typing import Any

import pytest

from entcommon.paging import TokenError, TokenMismatch, decode_token, encode_token

KEY = {"pk": "ART#a1", "gsi1pk": "FAILED_PERMANENT", "gsi1sk": "2026-10-11T00:00:00.000Z"}


def test_round_trip() -> None:
    token = encode_token(KEY)
    assert token is not None
    assert decode_token(token) == KEY


def test_wire_format_is_unpadded_urlsafe_sorted_compact_json() -> None:
    token = encode_token({"b": "?>", "a": "x"})
    raw = '{"a":"x","b":"?>"}'
    assert token == base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    assert "=" not in (token or "")
    assert encode_token({"a": "b"}) == "eyJhIjoiYiJ9"  # a token the services issue today


@pytest.mark.parametrize("key", [None, {}])
def test_no_next_page(key: dict[str, Any] | None) -> None:
    assert encode_token(key) is None


def test_decimals_are_plain_numbers() -> None:
    token = encode_token({"pk": "p", "n": Decimal(3), "f": Decimal("1.5")})
    assert token is not None
    padded = token + "=" * (-len(token) % 4)
    assert json.loads(base64.urlsafe_b64decode(padded)) == {"pk": "p", "n": 3, "f": 1.5}
    with pytest.raises(TokenError):  # only string keys come back in
        decode_token(token)


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "token",
    [
        "!!!",
        "%%%",
        "",
        "bm90IGpzb24",  # "not json"
        "WzFd",  # [1]
        _b64('"a string"'),
        _b64('{"pk": 1}'),
        _b64('{"pk": null}'),
        _b64('{"pk": {"S": "x"}}'),
        _b64("\xff"),
    ],
)
def test_garbage_is_refused(token: str) -> None:
    with pytest.raises(TokenError, match="next_token is not valid"):
        decode_token(token)


def test_tampered_token_is_refused() -> None:
    token = encode_token(KEY)
    assert token is not None
    with pytest.raises(TokenError):
        decode_token(token[:-3] + "!!!")


def test_error_is_a_value_error() -> None:
    assert issubclass(TokenError, ValueError)
    assert issubclass(TokenMismatch, TokenError)


ALLOWED = frozenset({"pk", "sk", "gsi1pk", "gsi1sk"})
REQUIRED = frozenset({"pk", "gsi1pk"})


@pytest.mark.parametrize(
    ("key", "ok"),
    [
        (KEY, True),
        ({"pk": "a", "gsi1pk": "b"}, True),
        ({"pk": "a", "sk": "s", "gsi1pk": "b", "gsi1sk": "c"}, True),
        ({"pk": "a"}, False),  # missing a required key
        ({"pk": "a", "gsi1pk": "b", "extra": "x"}, False),  # a key outside the shape
        ({"other": "x"}, False),
    ],
)
def test_key_shape(key: dict[str, str], ok: bool) -> None:
    token = encode_token(key)
    assert token is not None
    if ok:
        assert decode_token(token, expect_keys=ALLOWED, require_keys=REQUIRED) == key
    else:
        with pytest.raises(TokenError, match="not valid"):
            decode_token(token, expect_keys=ALLOWED, require_keys=REQUIRED)


def test_expect_keys_alone_bounds_the_shape() -> None:
    token = encode_token({"pk": "a"})
    assert token is not None
    assert decode_token(token, expect_keys={"pk", "sk"}) == {"pk": "a"}
    with pytest.raises(TokenError):
        decode_token(token, expect_keys={"sk"})


def test_check_binds_a_token_to_its_query() -> None:
    token = encode_token({"pk": "a", "member_process_id": "prc_1"})
    assert token is not None

    def own(key: dict[str, Any]) -> bool:
        return key.get("member_process_id") == "prc_1"

    assert decode_token(token, check=own)["pk"] == "a"
    with pytest.raises(TokenMismatch, match="belongs to another process"):
        decode_token(
            token,
            check=lambda k: k.get("member_process_id") == "prc_2",
            mismatch="next_token belongs to another process",
        )
    with pytest.raises(TokenError, match="belongs to another query"):
        decode_token(token, check=lambda _: False)


def test_check_runs_only_on_well_formed_tokens() -> None:
    calls: list[dict[str, Any]] = []

    def check(key: dict[str, Any]) -> bool:
        calls.append(key)
        return True

    with pytest.raises(TokenError, match="not valid"):
        decode_token("WzFd", check=check)
    assert calls == []
