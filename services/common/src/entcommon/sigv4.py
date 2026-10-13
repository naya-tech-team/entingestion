"""SigV4 signing for API Gateway (execute-api, AWS_IAM) calls."""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials, ReadOnlyCredentials

SERVICE = "execute-api"

# <api-id>.execute-api.<region>.amazonaws.com, or .amazonaws.com.cn in the China partition.
_EXECUTE_API_HOST = re.compile(
    r"\.execute-api\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$", re.IGNORECASE
)


def region_in_url(url: str) -> str | None:
    """The region of an execute-api URL (any partition), or None for another host."""
    host = urlsplit(url).hostname or ""
    match = _EXECUTE_API_HOST.search(host)
    return match.group(1).lower() if match else None


def region_from_url(url: str) -> str:
    """The region of an execute-api URL, else AWS_REGION / AWS_DEFAULT_REGION (e.g. a custom
    domain or a local test server). Raises ValueError when neither gives one."""
    region = (
        region_in_url(url) or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    )
    if not region:
        raise ValueError(f"no region in {url!r} and AWS_REGION is not set")
    return region


def signed_headers(
    method: str,
    url: str,
    body: bytes | None,
    credentials: Credentials | ReadOnlyCredentials,
    headers: dict[str, str] | None = None,
    *,
    region: str | None = None,
    service: str = SERVICE,
) -> dict[str, str]:
    """The request's headers plus host, X-Amz-Date, X-Amz-Security-Token (temporary credentials)
    and Authorization. The region defaults to region_from_url(url). Send the body unchanged and
    the URL exactly as signed (query already RFC 3986-encoded)."""
    frozen = (
        credentials.get_frozen_credentials()
        if isinstance(credentials, Credentials)
        else credentials
    )
    request = AWSRequest(
        method=method,
        url=url,
        data=body or b"",
        headers={"host": urlsplit(url).netloc, **(headers or {})},
    )
    SigV4Auth(frozen, service, region or region_from_url(url)).add_auth(request)
    return dict(request.headers.items())
