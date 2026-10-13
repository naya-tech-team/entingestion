"""SigV4 signing for execute-api, checked against an independent implementation of the spec."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from urllib.parse import parse_qsl, quote, urlsplit

import pytest
from botocore.credentials import Credentials, ReadOnlyCredentials

from entcommon import sigv4
from entcommon.sigv4 import region_from_url, region_in_url, signed_headers

AKID = "AKIDEXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"  # noqa: S105 - AWS docs example
TOKEN = "session-token-example"  # noqa: S105
NOW = datetime(2026, 10, 12, 8, 30, 0)  # botocore uses naive UTC
AMZ_DATE = "20261012T083000Z"
API = "https://abc123.execute-api.eu-west-1.amazonaws.com/prod"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("botocore.auth.get_current_datetime", lambda *a, **k: NOW)


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def reference_authorization(
    method: str, url: str, body: bytes, headers: dict[str, str], *, region: str, token: str | None
) -> str:
    """Signature Version 4 from the AWS spec, without botocore."""
    parts = urlsplit(url)
    hdrs = {k.lower(): v for k, v in headers.items()}
    hdrs |= {"host": parts.netloc, "x-amz-date": AMZ_DATE}
    if token:
        hdrs["x-amz-security-token"] = token
    names = sorted(hdrs)
    query = "&".join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
        for k, v in sorted(parse_qsl(parts.query, keep_blank_values=True))
    )
    canonical = "\n".join(
        [
            method,
            quote(parts.path or "/", safe="/~"),
            query,
            "".join(f"{n}:{' '.join(hdrs[n].split())}\n" for n in names),
            ";".join(names),
            hashlib.sha256(body).hexdigest(),
        ]
    )
    scope = f"{AMZ_DATE[:8]}/{region}/execute-api/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", AMZ_DATE, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = ("AWS4" + SECRET).encode()
    for part in (AMZ_DATE[:8], region, "execute-api", "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return (
        f"AWS4-HMAC-SHA256 Credential={AKID}/{scope}, "
        f"SignedHeaders={';'.join(names)}, Signature={signature}"
    )


@pytest.mark.parametrize(
    ("method", "path", "body", "headers"),
    [
        ("GET", "/v1/processes/prc_1", None, {}),
        ("GET", "/v1/processes?source=news&limit=20&next_token=eyJhIjoiYiJ9", None, {}),
        (
            "POST",
            "/v1/processes",
            b'{"name": "x", "source": "news"}',
            {"content-type": "application/json", "accept": "application/json"},
        ),
        ("DELETE", "/v1/articles/art_1", b"", {"accept": "application/json"}),
    ],
)
@pytest.mark.parametrize("token", [None, TOKEN])
def test_signature_matches_the_spec(
    method: str, path: str, body: bytes | None, headers: dict[str, str], token: str | None
) -> None:
    creds = ReadOnlyCredentials(AKID, SECRET, token)
    url = API + path

    out = signed_headers(method, url, body, creds, headers)

    expected = reference_authorization(
        method, url, body or b"", headers, region="eu-west-1", token=token
    )
    assert out["Authorization"] == expected
    assert out["X-Amz-Date"] == AMZ_DATE
    assert out["host"] == "abc123.execute-api.eu-west-1.amazonaws.com"
    assert out.get("X-Amz-Security-Token") == token
    for name, value in headers.items():
        assert out[name] == value


def test_deterministic_and_accepts_refreshable_credentials() -> None:
    url = API + "/v1/processes/prc_1"
    frozen = signed_headers("GET", url, None, ReadOnlyCredentials(AKID, SECRET, TOKEN))
    live = signed_headers("GET", url, None, Credentials(AKID, SECRET, TOKEN))
    assert frozen == live
    assert frozen == signed_headers("GET", url, None, ReadOnlyCredentials(AKID, SECRET, TOKEN))


def test_explicit_region_and_service() -> None:
    url = "http://127.0.0.1:8080/prod/v1/x"
    out = signed_headers(
        "GET", url, None, ReadOnlyCredentials(AKID, SECRET, None), region="us-east-2"
    )
    assert "/20261012/us-east-2/execute-api/aws4_request" in out["Authorization"]
    assert out["host"] == "127.0.0.1:8080"
    other = signed_headers(
        "GET", url, None, ReadOnlyCredentials(AKID, SECRET, None), region="r", service="lambda"
    )
    assert "/r/lambda/aws4_request" in other["Authorization"]


def test_caller_headers_are_not_mutated() -> None:
    headers = {"accept": "application/json"}
    signed_headers("GET", API + "/x", None, ReadOnlyCredentials(AKID, SECRET, None), headers)
    assert headers == {"accept": "application/json"}


@pytest.mark.parametrize(
    ("url", "region"),
    [
        ("https://abc.execute-api.us-west-2.amazonaws.com/prod", "us-west-2"),
        ("https://abc.execute-api.eu-central-1.amazonaws.com", "eu-central-1"),
        ("https://abc.execute-api.cn-north-1.amazonaws.com.cn/prod/v1", "cn-north-1"),
        ("https://abc.execute-api.us-gov-west-1.amazonaws.com/prod", "us-gov-west-1"),
        ("https://ABC.EXECUTE-API.US-EAST-1.AMAZONAWS.COM/prod", "us-east-1"),
        ("https://abc.execute-api.us-west-2.amazonaws.com:443/prod", "us-west-2"),
        ("https://api.example.com/prod", None),
        ("https://execute-api.us-west-2.amazonaws.com.evil.example/prod", None),
        ("http://127.0.0.1:1/prod", None),
        ("not a url", None),
    ],
)
def test_region_in_url(url: str, region: str | None) -> None:
    assert region_in_url(url) == region


def test_region_from_url_falls_back_to_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    url = "http://127.0.0.1:1/prod"
    with pytest.raises(ValueError, match="no region"):
        region_from_url(url)
    with pytest.raises(ValueError, match="no region"):
        signed_headers("GET", url, None, ReadOnlyCredentials(AKID, SECRET, None))
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-2")
    assert region_from_url(url) == "eu-west-2"
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    assert region_from_url(url) == "ap-south-1"
    assert region_from_url(API) == "eu-west-1"  # the URL wins


def test_service_name() -> None:
    assert sigv4.SERVICE == "execute-api"
