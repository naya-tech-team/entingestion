"""SigV4-signed calls to the producer and control APIs (AWS_IAM) as the function role."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import quote, urlencode, urlsplit

import botocore.session
import urllib3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

# CloudFront gives the origin 30 s; Lambda is stopped at its own timeout just under that.
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 25.0

_EXECUTE_API_HOST = re.compile(r"\.execute-api\.([a-z0-9-]+)\.amazonaws\.com$", re.IGNORECASE)


class UpstreamError(Exception):
    """The API couldn't be reached or didn't answer in time."""


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    body: bytes
    retry_after: str | None = None


@lru_cache(maxsize=1)
def _session() -> botocore.session.Session:
    return botocore.session.get_session()


@lru_cache(maxsize=1)
def _pool() -> urllib3.PoolManager:
    return urllib3.PoolManager(num_pools=4, maxsize=10)


def region_of(url: str) -> str:
    """The API's region from its execute-api host, else the function's own region."""
    host = urlsplit(url).hostname or ""
    match = _EXECUTE_API_HOST.search(host)
    if match:
        return match.group(1)
    return os.environ.get("AWS_REGION") or os.environ["AWS_DEFAULT_REGION"]


def build_url(base_url: str, path: str, query: list[tuple[str, str]] | None = None) -> str:
    """base_url (the stage invoke URL) + path, with the query RFC 3986-encoded as SigV4 expects."""
    url = base_url.rstrip("/") + path
    if query:
        url += "?" + urlencode(query, quote_via=quote, safe="-_.~")
    return url


def call(
    method: str,
    base_url: str,
    path: str,
    *,
    query: list[tuple[str, str]] | None = None,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> UpstreamResponse:
    """Sign the request with the function role's credentials and send it (no retries)."""
    url = build_url(base_url, path, query)
    request = AWSRequest(
        method=method,
        url=url,
        data=body or b"",
        headers={"host": urlsplit(url).netloc, "accept": "application/json", **(headers or {})},
    )
    credentials = _session().get_credentials()
    if credentials is None:
        raise RuntimeError("no AWS credentials for signing")
    SigV4Auth(credentials.get_frozen_credentials(), "execute-api", region_of(url)).add_auth(request)
    try:
        response = _pool().request(
            method,
            url,
            body=body,
            headers=dict(request.headers.items()),
            timeout=urllib3.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT),
            retries=False,
            redirect=False,
        )
    except urllib3.exceptions.HTTPError as exc:
        raise UpstreamError(type(exc).__name__) from exc
    return UpstreamResponse(
        status=response.status,
        body=response.data,
        retry_after=response.headers.get("retry-after"),
    )
