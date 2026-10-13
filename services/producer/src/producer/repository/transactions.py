"""The producer's transaction retry policy.

Registrars counting into the same process row at once cancel each other's transactions with
TransactionConflict. That's contention, not a failed condition: retry with jitter.
"""

from __future__ import annotations

import random
import time
from typing import Any

from botocore.exceptions import ClientError
from entcommon.dynamo import cancellation_codes

CONFLICT_ATTEMPTS = 6
CONFLICT_BASE_S = 0.05


def transact_with_retry(client: Any, items: list[dict[str, Any]]) -> None:
    """transact_write_items, retrying TransactionConflict. Other errors propagate."""
    for attempt in range(CONFLICT_ATTEMPTS):
        try:
            client.transact_write_items(TransactItems=items)
        except ClientError as err:
            codes = cancellation_codes(err) or []
            if "TransactionConflict" not in codes or "ConditionalCheckFailed" in codes:
                raise
            if attempt == CONFLICT_ATTEMPTS - 1:
                raise
            time.sleep(random.uniform(0, CONFLICT_BASE_S * 2**attempt))  # noqa: S311
        else:
            return


def condition_failed(err: ClientError, *, first_only: bool = False) -> bool:
    """Whether a cancelled transaction failed a condition: any item's, or (first_only) the
    first item's."""
    codes = cancellation_codes(err)
    if not codes:
        return False
    return codes[0] == "ConditionalCheckFailed" if first_only else "ConditionalCheckFailed" in codes
