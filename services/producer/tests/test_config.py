from dataclasses import replace
from datetime import timedelta

import pytest

from producer.config import (
    DEFAULT_ALLOWED_CONTENT_TYPES,
    DEFAULT_MAX_FILE_BYTES,
    MissingSetting,
    Settings,
)

REQUIRED = {
    "PROCESS_TABLE": "process",
    "ARTICLES_TABLE": "articles",
    "UPLOADS_TABLE": "producer_uploads",
    "RAW_BUCKET": "raw-bucket",
}


def test_settings_defaults() -> None:
    s = Settings.from_env(REQUIRED)

    assert s.raw_prefix == "raw/"
    assert s.max_file_bytes == DEFAULT_MAX_FILE_BYTES
    assert s.upload_url_ttl_seconds == 900
    assert s.allowed_content_types == DEFAULT_ALLOWED_CONTENT_TYPES
    assert s.upload_row_ttl == timedelta(days=15)
    assert s.finished_upload_ttl == timedelta(days=30)
    assert s.ticket_grace == timedelta(hours=1)
    assert (s.janitor_batch, s.open_process_idle_days) == (500, 7)
    assert (s.idempotency_table, s.registrar_queue_url) == (None, None)


def test_settings_overrides() -> None:
    s = Settings.from_env(
        REQUIRED
        | {
            "MAX_FILE_BYTES": "1024",
            "ALLOWED_CONTENT_TYPES": "text/plain, application/pdf",
            "UPLOAD_ROW_TTL_DAYS": "20",
            "FINISHED_UPLOAD_TTL_DAYS": "40",
            "TICKET_GRACE_SECONDS": "60",
            "JANITOR_BATCH": "50",
            "OPEN_PROCESS_IDLE_DAYS": "2",
            "IDEMPOTENCY_TABLE": "idem",
            "REGISTRAR_QUEUE_URL": "https://sqs.example/q",
        }
    )

    assert s.max_file_bytes == 1024
    assert s.allowed_content_types == frozenset({"text/plain", "application/pdf"})
    assert s.upload_row_ttl == timedelta(days=20)
    assert s.finished_upload_ttl == timedelta(days=40)
    assert s.ticket_grace == timedelta(seconds=60)
    assert (s.janitor_batch, s.open_process_idle_days) == (50, 2)
    assert s.need("IDEMPOTENCY_TABLE") == "idem"
    assert s.require("IDEMPOTENCY_TABLE", "REGISTRAR_QUEUE_URL") is s


@pytest.mark.parametrize("name", sorted(REQUIRED))
def test_tables_and_bucket_are_required(name: str) -> None:
    env = {k: v for k, v in REQUIRED.items() if k != name}

    with pytest.raises(MissingSetting, match=name):
        Settings.from_env(env)


def test_functions_name_the_optional_settings_they_need() -> None:
    s = Settings.from_env(REQUIRED)

    with pytest.raises(MissingSetting, match="REGISTRAR_QUEUE_URL"):
        s.require("REGISTRAR_QUEUE_URL")
    with pytest.raises(MissingSetting, match="IDEMPOTENCY_TABLE"):
        s.need("IDEMPOTENCY_TABLE")
    with pytest.raises(ValueError, match="unknown"):
        s.need("NO_SUCH_SETTING")
    assert replace(s, registrar_queue_url="q").need("REGISTRAR_QUEUE_URL") == "q"
