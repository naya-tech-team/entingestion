from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from ingest.errors import (
    ErrorClass,
    ErrorCode,
    IngestPermanent,
    IngestSkip,
    IngestThrottled,
    IngestTransient,
    classify,
    error_class_of,
    make_error,
    parse_cause,
    validation_code,
)


def client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "Op",
    )


@pytest.mark.parametrize(
    ("exc", "code", "cls"),
    [
        (client_error("ThrottlingException"), ErrorCode.THROTTLED, IngestThrottled),
        (client_error("Anything", 429), ErrorCode.THROTTLED, IngestThrottled),
        (client_error("ServiceUnavailableException", 503), ErrorCode.UPSTREAM_5XX, IngestTransient),
        (client_error("ModelTimeoutException", 408), ErrorCode.TIMEOUT, IngestTransient),
        (client_error("NoSuchKey", 404), ErrorCode.SOURCE_MISSING, IngestPermanent),
        (client_error("AccessDenied", 403), ErrorCode.BUG, IngestPermanent),
        (ReadTimeoutError(endpoint_url="https://x"), ErrorCode.TIMEOUT, IngestTransient),
        (ValueError("boom"), ErrorCode.BUG, IngestPermanent),
    ],
)
def test_classify(exc: Exception, code: ErrorCode, cls: type) -> None:
    err = classify(exc)
    assert err.code == code
    assert type(err) is cls


def test_classify_psycopg_without_importing_it() -> None:
    import psycopg

    conflict = psycopg.errors.SerializationFailure("could not serialize")
    assert classify(conflict).code == ErrorCode.DB_CONFLICT
    assert classify(psycopg.OperationalError("proxy busy")).code == ErrorCode.DB_CONN
    timeout = psycopg.errors.ConnectionTimeout("connection timeout expired")
    assert classify(timeout).code == ErrorCode.DB_CONN
    assert type(classify(timeout)).__name__ == "IngestTransient"
    assert classify(psycopg.errors.QueryCanceled("timeout")).code == ErrorCode.TIMEOUT


def test_a_missing_table_or_column_is_retried() -> None:
    """Code that ran before its migration: retried, so runs recover after `make migrate`."""
    import psycopg

    for exc in (
        psycopg.errors.UndefinedTable('relation "chunks_new" does not exist'),
        psycopg.errors.UndefinedColumn('column "tenant_id" does not exist'),
    ):
        err = classify(exc)
        assert err.code == ErrorCode.DB_SCHEMA
        assert type(err).__name__ == "IngestTransient"


def test_classes_and_factory() -> None:
    assert error_class_of(validation_code("dimension")) is ErrorClass.PERMANENT
    assert validation_code("dimension") == "E_VALIDATION_DIMENSION"
    assert isinstance(make_error(ErrorCode.EMPTY_TEXT), IngestSkip)
    assert isinstance(make_error(ErrorCode.TOO_LARGE), IngestPermanent)  # big PDFs go to OCR
    assert isinstance(make_error("E_SOMETHING_NEW"), IngestPermanent)  # unknown -> permanent


def test_errors_keep_their_identity_through_classify() -> None:
    err = IngestSkip(ErrorCode.EMPTY_TEXT, "no text")
    assert classify(err) is err


def test_parse_cause_round_trip_through_step_functions() -> None:
    err = IngestPermanent(ErrorCode.UNREADABLE, "encrypted PDF")
    # What a Lambda failure looks like in a Catch's $.Cause.
    cause = json.dumps({"errorType": "IngestPermanent", "errorMessage": str(err)})

    assert parse_cause(cause) == {"code": "E_UNREADABLE", "detail": "encrypted PDF"}
    assert parse_cause(None)["code"] == "E_BUG"
    assert parse_cause("not json")["code"] == "E_BUG"


def test_detail_is_capped() -> None:
    assert len(IngestPermanent(ErrorCode.BUG, "x" * 5000).detail) == 1024
