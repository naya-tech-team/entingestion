import pytest

from bff.config import MissingSetting, Settings

URLS = {"PRODUCER_API_URL": "https://p.example/prod", "CONTROL_API_URL": "https://c.example/v"}


def test_defaults() -> None:
    s = Settings.from_env(URLS)

    assert (s.producer_api_url, s.control_api_url) == tuple(URLS.values())
    assert (s.max_rerun_ids, s.max_redrive) == (100, 200)
    assert (s.connect_timeout, s.read_timeout) == (3.0, 25.0)


def test_overrides() -> None:
    s = Settings.from_env(
        URLS
        | {
            "MAX_RERUN_IDS": "10",
            "MAX_REDRIVE": "20",
            "CONNECT_TIMEOUT": "1",
            "READ_TIMEOUT": "9.5",
        }
    )

    assert (s.max_rerun_ids, s.max_redrive, s.connect_timeout, s.read_timeout) == (10, 20, 1, 9.5)


def test_both_urls_are_required() -> None:
    with pytest.raises(MissingSetting, match="PRODUCER_API_URL, CONTROL_API_URL"):
        Settings.from_env({})
    with pytest.raises(MissingSetting, match="CONTROL_API_URL"):
        Settings.from_env({"PRODUCER_API_URL": "https://p.example", "CONTROL_API_URL": ""})
