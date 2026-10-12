"""Client for the producer API and presigned S3 uploads."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import boto3
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from producer_client.sigv4 import Signer, region_from_url

CHUNK = 8 * 1024 * 1024
PUT_ATTEMPTS = 3
TIMEOUT = (10, 300)

_EXTRA_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class ApiError(Exception):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"HTTP {status}: {json.dumps(body)}")
        self.status = status
        self.body = body


class UploadError(Exception):
    pass


@dataclass(frozen=True)
class FileInfo:
    path: Path
    size: int
    sha256: str
    content_type: str


def guess_content_type(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in _EXTRA_TYPES:
        return _EXTRA_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed


def describe_file(path: Path) -> FileInfo:
    content_type = guess_content_type(path)
    if content_type is None:
        raise UploadError(f"{path}: cannot determine content type")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return FileInfo(path=path, size=size, sha256=digest.hexdigest(), content_type=content_type)


class ProducerClient:
    def __init__(
        self,
        api_url: str,
        *,
        region: str | None = None,
        session: boto3.Session | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.session = session or boto3.Session()
        resolved = region or region_from_url(self.api_url) or self.session.region_name
        if not resolved:
            raise ValueError("cannot determine the AWS region; pass --region")
        self.signer = Signer(self.session, resolved)

        # API calls are retried; POSTs carry an Idempotency-Key, so a retry after a lost response
        # gets the first answer instead of creating a duplicate.
        self.http = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "PATCH", "DELETE", "POST"}),
        )
        self.http.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=32))
        # S3 PUTs: retried by hand, because each attempt must reopen the file.
        self.s3_http = requests.Session()
        self.s3_http.mount("https://", HTTPAdapter(pool_maxsize=32))

    # -- transport ----------------------------------------------------------

    def _send(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        url = self.api_url + path
        if query:
            url += "?" + urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if method == "POST":
            headers["Idempotency-Key"] = uuid.uuid4().hex
        signed = self.signer.sign(method, url, data, headers)
        resp = self.http.request(method, url, data=data, headers=signed, timeout=TIMEOUT)
        return resp.status_code, (resp.json() if resp.content else None)

    def _call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        status, payload = self._send(method, path, body, query)
        if status >= 400:  # noqa: PLR2004
            raise ApiError(status, payload)
        return payload

    # -- API ----------------------------------------------------------------

    def create_process(
        self,
        name: str,
        source: str,
        expected_count: int | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "source": source}
        if expected_count is not None:
            body["expected_count"] = expected_count
        if metadata:
            body["metadata"] = metadata
        return dict(self._call("POST", "/v1/processes", body))

    def get_process(self, process_id: str) -> dict[str, Any]:
        return dict(self._call("GET", f"/v1/processes/{process_id}"))

    def close_process(self, process_id: str, expected_count: int | None = None) -> dict[str, Any]:
        body = {} if expected_count is None else {"expected_count": expected_count}
        return dict(self._call("POST", f"/v1/processes/{process_id}/close", body))

    def create_upload(
        self,
        process_id: str,
        info: FileInfo,
        *,
        filename: str | None = None,
        external_id: str | None = None,
        title: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "filename": filename or info.path.name,
            "content_type": info.content_type,
            "size_bytes": info.size,
            "sha256": info.sha256,
        }
        for key, value in (("external_id", external_id), ("title", title), ("metadata", metadata)):
            if value is not None:
                body[key] = value
        return dict(self._call("POST", f"/v1/processes/{process_id}/uploads", body))

    def list_articles(
        self, process_id: str, limit: int = 100, next_token: str | None = None
    ) -> dict[str, Any]:
        query = {"limit": limit, "next_token": next_token}
        return dict(self._call("GET", f"/v1/processes/{process_id}/articles", query=query))

    def get_upload(self, upload_id: str) -> dict[str, Any]:
        return dict(self._call("GET", f"/v1/uploads/{upload_id}"))

    def iter_articles(self, process_id: str, page_size: int = 500) -> Iterator[dict[str, Any]]:
        """Every article of a process, following next_token."""
        token: str | None = None
        while True:
            page = self.list_articles(process_id, page_size, token)
            yield from page["items"]
            token = page.get("next_token")
            if not token:
                return

    def wait_settled(
        self, process_id: str, timeout: float = 900, interval: float = 5
    ) -> dict[str, Any]:
        """Poll until the process is settled (closed and every upload has an outcome)."""
        deadline = time.monotonic() + timeout
        while True:
            process = self.get_process(process_id)
            if process.get("settled"):
                return process
            if time.monotonic() >= deadline:
                raise TimeoutError(f"process {process_id} not settled after {timeout:.0f} s")
            time.sleep(interval)

    # -- S3 -----------------------------------------------------------------

    def put_file(self, ticket: dict[str, Any], info: FileInfo) -> None:
        """PUT the file to the presigned URL with exactly the ticket's headers."""
        headers = dict(ticket["headers"])
        # An explicit length stops requests from falling back to chunked encoding,
        # which presigned PUTs don't accept.
        headers["Content-Length"] = str(info.size)
        last_error = ""
        for attempt in range(1, PUT_ATTEMPTS + 1):
            try:
                with info.path.open("rb") as fh:
                    # Never follow redirects: the signature is bound to the presigned host.
                    resp = self.s3_http.put(
                        ticket["url"],
                        data=fh,
                        headers=headers,
                        timeout=TIMEOUT,
                        allow_redirects=False,
                    )
            except requests.RequestException as exc:
                last_error = str(exc)
            else:
                if resp.status_code < 300:  # noqa: PLR2004
                    return
                last_error = f"HTTP {resp.status_code}: {resp.text[:500]}"
                if resp.status_code < 500:  # noqa: PLR2004
                    break  # client errors (bad checksum, expired URL) won't succeed on retry
            time.sleep(min(2**attempt, 10))
        raise UploadError(f"{info.path}: upload failed: {last_error}")

    def upload_file(
        self,
        process_id: str,
        path: Path,
        *,
        external_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        info = describe_file(path)
        ticket = self.create_upload(process_id, info, external_id=external_id, title=title)
        self.put_file(ticket, info)
        return ticket
