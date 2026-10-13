"""SigV4-signed calls to the producer and control APIs (AWS_IAM) as the function role."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import quote, urlencode

import botocore.session
import urllib3
from entcommon.sigv4 import signed_headers


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
    timeout: urllib3.Timeout,
    query: list[tuple[str, str]] | None = None,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> UpstreamResponse:
    """Sign the request with the function role's credentials and send it (no retries). The
    region comes from the execute-api host, else the function's own (AWS_REGION)."""
    url = build_url(base_url, path, query)
    credentials = _session().get_credentials()
    if credentials is None:
        raise RuntimeError("no AWS credentials for signing")
    signed = signed_headers(
        method, url, body, credentials, {"accept": "application/json", **(headers or {})}
    )
    try:
        response = _pool().request(
            method,
            url,
            body=body,
            headers=signed,
            timeout=timeout,
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
