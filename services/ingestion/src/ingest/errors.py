"""Error taxonomy and classifier (brief §9).

Step Functions matches Retry/Catch on the Lambda error type, which is the exception class name.
Every step raises one of the IngestError subclasses below, so the ASL can say, for example:

    "Retry": [{"ErrorEquals": ["IngestThrottled", "Lambda.TooManyRequestsException"],
               "IntervalSeconds": 5, "MaxDelaySeconds": 120, "MaxAttempts": 12, ...},
              {"ErrorEquals": ["IngestTransient"], "MaxAttempts": 3, ...}],
    "Catch": [{"ErrorEquals": ["States.ALL"], "Next": "MarkFailed"}]

The class names are a contract with asl/*.asl.json.tftpl; don't rename them.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

ERROR_MESSAGE_MAX = 1024


class ErrorClass(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    SKIP = "skip"
    ROUTE = "route"


class ErrorCode(StrEnum):
    THROTTLED = "E_THROTTLED"
    UPSTREAM_5XX = "E_UPSTREAM_5XX"
    # An embedding endpoint refused the request (HTTP 4xx other than 429: bad model, key or
    # input). Permanent: retrying the same request won't help.
    UPSTREAM_4XX = "E_UPSTREAM_4XX"
    TIMEOUT = "E_TIMEOUT"
    DB_CONFLICT = "E_DB_CONFLICT"
    DB_CONN = "E_DB_CONN"
    # A table or column the code needs isn't there (yet): code deployed before its migration.
    # Retried, so runs recover once `make migrate` has run (docs/RUNBOOK.md "Deploy").
    DB_SCHEMA = "E_DB_SCHEMA"
    SOURCE_MISSING = "E_SOURCE_MISSING"
    SOURCE_CHANGED = "E_SOURCE_CHANGED"
    UNREADABLE = "E_UNREADABLE"
    EMPTY_TEXT = "E_EMPTY_TEXT"
    PII_BLOCKED = "E_PII_BLOCKED"  # PII found and the source's policy is block (ingest.pii)
    TOO_LARGE = "E_TOO_LARGE"
    LEASE_BUSY = "E_LEASE_BUSY"  # another run holds the article; retried
    VALIDATION = "E_VALIDATION"  # always suffixed: E_VALIDATION_<CHECK>
    BUG = "E_BUG"


def validation_code(check: str) -> str:
    return f"{ErrorCode.VALIDATION.value}_{check.upper()}"


def error_class_of(code: str) -> ErrorClass:
    if code.startswith(ErrorCode.VALIDATION.value):
        return ErrorClass.PERMANENT
    return _CLASS_BY_CODE.get(code, ErrorClass.PERMANENT)


_CLASS_BY_CODE: dict[str, ErrorClass] = {
    ErrorCode.THROTTLED: ErrorClass.TRANSIENT,
    ErrorCode.UPSTREAM_5XX: ErrorClass.TRANSIENT,
    ErrorCode.UPSTREAM_4XX: ErrorClass.PERMANENT,
    ErrorCode.TIMEOUT: ErrorClass.TRANSIENT,
    ErrorCode.DB_CONFLICT: ErrorClass.TRANSIENT,
    ErrorCode.DB_CONN: ErrorClass.TRANSIENT,
    ErrorCode.DB_SCHEMA: ErrorClass.TRANSIENT,
    ErrorCode.SOURCE_MISSING: ErrorClass.PERMANENT,
    ErrorCode.SOURCE_CHANGED: ErrorClass.PERMANENT,
    ErrorCode.UNREADABLE: ErrorClass.PERMANENT,
    ErrorCode.EMPTY_TEXT: ErrorClass.SKIP,
    ErrorCode.PII_BLOCKED: ErrorClass.SKIP,
    # Fetch already routes big PDFs to OCR; what's still too large can't be processed.
    ErrorCode.TOO_LARGE: ErrorClass.PERMANENT,
    ErrorCode.LEASE_BUSY: ErrorClass.TRANSIENT,
    ErrorCode.BUG: ErrorClass.PERMANENT,
}


class IngestError(Exception):
    """Base class. The message is JSON so MarkFailed can parse $.Cause back into code + detail."""

    error_class: ErrorClass = ErrorClass.PERMANENT

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:ERROR_MESSAGE_MAX]
        super().__init__(json.dumps({"code": code, "detail": self.detail}))


class IngestThrottled(IngestError):
    """Retried with backoff and full jitter (5 s doubling to 120 s, 12 attempts), then MarkFailed
    makes it FAILED_RETRYABLE on its own budget (throttle_count), not retry_count."""

    error_class = ErrorClass.TRANSIENT


class IngestTransient(IngestError):
    """Retried 3 times, then FAILED_RETRYABLE (the reconciler re-queues later)."""

    error_class = ErrorClass.TRANSIENT


class IngestPermanent(IngestError):
    """Not retried. FAILED_PERMANENT, alarm, replayable after a fix."""

    error_class = ErrorClass.PERMANENT


class IngestSkip(IngestError):
    """Nothing to index (e.g. no text). SKIPPED, terminal, counted as skipped."""

    error_class = ErrorClass.SKIP


class IngestRoute(IngestError):
    """Too large for Lambda; the state machine routes the work to Fargate."""

    error_class = ErrorClass.ROUTE


class IngestStale(Exception):  # the class name is matched by the state machine
    """This run lost its lease (another run owns the article). Not a failure: the state machine
    catches it and ends quietly (brief §6.3 step 1, test CONC)."""


class DeletePending(IngestStale):
    """The producer deleted the article while this run held it. The run hands the article over
    to the delete and then ends as stale (never raised to the state machine: its class name
    isn't caught there)."""


_EXCEPTION_BY_CLASS: dict[ErrorClass, type[IngestError]] = {
    ErrorClass.PERMANENT: IngestPermanent,
    ErrorClass.SKIP: IngestSkip,
    ErrorClass.ROUTE: IngestRoute,
}


def make_error(code: str, detail: str = "") -> IngestError:
    """The IngestError subclass that matches a code's class."""
    if code == ErrorCode.THROTTLED:
        return IngestThrottled(code, detail)
    cls = error_class_of(code)
    if cls is ErrorClass.TRANSIENT:
        return IngestTransient(code, detail)
    return _EXCEPTION_BY_CLASS[cls](code, detail)


_THROTTLE_CODES = frozenset(
    {
        "ThrottlingException",
        "Throttling",
        "TooManyRequestsException",
        "ProvisionedThroughputExceededException",
        "RequestLimitExceeded",
        "SlowDown",
    }
)
_UPSTREAM_CODES = frozenset(
    {
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "InternalServerError",
        "InternalServerException",
        "InternalFailure",
        "ModelNotReadyException",
    }
)
_TIMEOUT_CODES = frozenset({"RequestTimeout", "RequestTimeoutException", "ModelTimeoutException"})
_MISSING_CODES = frozenset({"NoSuchKey", "NoSuchVersion", "404", "NotFound"})

# PostgreSQL SQLSTATEs: serialization_failure, deadlock_detected, lock_not_available.
_PG_CONFLICT = frozenset({"40001", "40P01", "55P03"})
_PG_TIMEOUT = frozenset({"57014"})  # query_canceled (statement_timeout)
_PG_SCHEMA = frozenset({"42P01", "42703"})  # undefined_table, undefined_column


def classify(exc: BaseException) -> IngestError:  # noqa: PLR0911 - one return per category
    """Map any exception to an IngestError. Unknown errors are permanent (E_BUG), never retried
    forever."""
    if isinstance(exc, IngestError):
        return exc
    detail = f"{type(exc).__name__}: {exc}"

    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        if code in _THROTTLE_CODES or status == 429:  # noqa: PLR2004
            return make_error(ErrorCode.THROTTLED, detail)
        if code in _TIMEOUT_CODES:
            return make_error(ErrorCode.TIMEOUT, detail)
        if code in _UPSTREAM_CODES or status >= 500:  # noqa: PLR2004
            return make_error(ErrorCode.UPSTREAM_5XX, detail)
        if code in _MISSING_CODES:
            return make_error(ErrorCode.SOURCE_MISSING, detail)
        return make_error(ErrorCode.BUG, detail)

    if isinstance(exc, ConnectTimeoutError | ReadTimeoutError | TimeoutError):
        return make_error(ErrorCode.TIMEOUT, detail)

    # psycopg is only in the `core` Lambda group, so match it without importing it.
    if type(exc).__module__.startswith("psycopg"):
        pg_code = _postgres_code(exc)
        if pg_code is not None:
            return make_error(pg_code, detail)

    if isinstance(exc, ConnectionError):
        return make_error(ErrorCode.UPSTREAM_5XX, detail)

    return make_error(ErrorCode.BUG, detail)


def _postgres_code(exc: BaseException) -> ErrorCode | None:
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate in _PG_CONFLICT:
        return ErrorCode.DB_CONFLICT
    if sqlstate in _PG_TIMEOUT:
        return ErrorCode.TIMEOUT
    if sqlstate in _PG_SCHEMA:
        return ErrorCode.DB_SCHEMA
    # Subclasses too, e.g. ConnectionTimeout is an OperationalError.
    if any(c.__name__ in {"OperationalError", "InterfaceError"} for c in type(exc).__mro__):
        return ErrorCode.DB_CONN
    return None


def parse_cause(cause: str | None) -> dict[str, Any]:
    """Recover {code, detail} from a Step Functions Catch $.Cause (Lambda error JSON)."""
    if not cause:
        return {"code": ErrorCode.BUG.value, "detail": "no cause"}
    try:
        outer = json.loads(cause)
        message = outer.get("errorMessage", cause) if isinstance(outer, dict) else cause
        inner = json.loads(message)
        if isinstance(inner, dict) and "code" in inner:
            return {"code": str(inner["code"]), "detail": str(inner.get("detail", ""))}
    except (ValueError, TypeError):
        pass
    return {"code": ErrorCode.BUG.value, "detail": str(cause)[:ERROR_MESSAGE_MAX]}
