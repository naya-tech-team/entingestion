import pytest

from producer.config import DEFAULT_ALLOWED_CONTENT_TYPES, DEFAULT_MAX_FILE_BYTES, Settings

REQUIRED = {
    "PROCESS_TABLE": "process",
    "ARTICLES_TABLE": "articles",
    "UPLOADS_TABLE": "producer_uploads",
    "RAW_BUCKET": "raw-bucket",
}


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    for key in ("MAX_FILE_BYTES", "ALLOWED_CONTENT_TYPES", "UPLOAD_URL_TTL_SECONDS", "RAW_PREFIX"):
        monkeypatch.delenv(key, raising=False)

    s = Settings.from_env()

    assert s.raw_prefix == "raw/"
    assert s.max_file_bytes == DEFAULT_MAX_FILE_BYTES
    assert s.upload_url_ttl_seconds == 900
    assert s.allowed_content_types == DEFAULT_ALLOWED_CONTENT_TYPES


def test_settings_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MAX_FILE_BYTES", "1024")
    monkeypatch.setenv("ALLOWED_CONTENT_TYPES", "text/plain, application/pdf")

    s = Settings.from_env()

    assert s.max_file_bytes == 1024
    assert s.allowed_content_types == frozenset({"text/plain", "application/pdf"})
