"""Opaque next_token values for paged DynamoDB queries.

A token is the query's LastEvaluatedKey as compact JSON with sorted keys, urlsafe base64 without
padding. Decoding accepts only a JSON object of string values, optionally of a given key shape and
bound to the query it came from (a token from another process or status is refused).
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Mapping, Set
from typing import Any

from entcommon.dynamo import plain

INVALID = "next_token is not valid"


class TokenError(ValueError):
    """The token isn't one this API issued (or not for this query)."""


class TokenMismatch(TokenError):
    """A well-formed token that belongs to another query."""


def encode_token(key: Mapping[str, Any] | None) -> str | None:
    """The token for a LastEvaluatedKey; None when there is no next page."""
    if not key:
        return None
    raw = json.dumps(plain(key), separators=(",", ":"), sort_keys=True, default=str)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_token(
    token: str,
    *,
    expect_keys: Set[str] | None = None,
    require_keys: Set[str] = frozenset(),
    check: Callable[[dict[str, Any]], bool] | None = None,
    mismatch: str = "next_token belongs to another query",
) -> dict[str, Any]:
    """The ExclusiveStartKey in a token.

    expect_keys: the keys the token may hold (None: any). require_keys: keys it must hold.
    check: whether the key belongs to this query; False raises TokenMismatch(mismatch).
    Anything else malformed raises TokenError("next_token is not valid").
    """
    try:
        value = json.loads(base64.urlsafe_b64decode((token + "=" * (-len(token) % 4)).encode()))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise TokenError(INVALID) from exc
    if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
        raise TokenError(INVALID)
    keys = value.keys()
    if not require_keys <= keys or (expect_keys is not None and not keys <= expect_keys):
        raise TokenError(INVALID)
    if check is not None and not check(value):
        raise TokenMismatch(mismatch)
    return value
