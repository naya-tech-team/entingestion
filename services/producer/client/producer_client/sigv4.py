"""SigV4 signing for API Gateway (execute-api) calls with the caller's AWS credentials."""

from __future__ import annotations

import boto3
from entcommon.sigv4 import region_in_url, signed_headers

__all__ = ["Signer", "region_in_url"]


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
        return signed_headers(method, url, body, credentials, headers, region=self.region)
