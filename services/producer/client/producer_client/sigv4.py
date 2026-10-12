"""SigV4 signing for API Gateway (execute-api) calls with the caller's AWS credentials."""

from __future__ import annotations

import re

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

_EXECUTE_API_HOST = re.compile(r"\.execute-api\.([a-z0-9-]+)\.amazonaws\.com", re.IGNORECASE)


def region_from_url(url: str) -> str | None:
    match = _EXECUTE_API_HOST.search(url)
    return match.group(1) if match else None


class Signer:
    def __init__(self, session: boto3.Session, region: str) -> None:
        self.session = session
        self.region = region

    def sign(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> dict[str, str]:
        """Return headers (including Authorization) for the request."""
        credentials = self.session.get_credentials()
        if credentials is None:
            raise RuntimeError("no AWS credentials found; configure a profile or environment")
        request = AWSRequest(method=method, url=url, data=body or b"", headers=headers)
        SigV4Auth(credentials.get_frozen_credentials(), "execute-api", self.region).add_auth(
            request
        )
        return dict(request.headers.items())
