from __future__ import annotations

import base64
import contextlib
import json
import os
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import botocore.session
import pytest


def _test_region() -> str:
    """The region the tests run in, from the environment: AWS_REGION, else AWS_DEFAULT_REGION,
    else the AWS config's region. moto works in any region; nothing here names one."""
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    region = region or botocore.session.get_session().get_config_variable("region")
    if not region:
        raise RuntimeError("set AWS_REGION (or AWS_DEFAULT_REGION) to run the tests")
    return region


REGION = _test_region()


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class FakeApi:
    """A local HTTP server standing in for an API stage: records requests, replies as told."""

    server: ThreadingHTTPServer
    requests: list[Recorded] = field(default_factory=list)
    status: int = 200
    body: Any = field(default_factory=lambda: {"ok": True})
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host!s}:{port}/prod"

    @property
    def last(self) -> Recorded:
        return self.requests[-1]


def _start(api_holder: list[FakeApi]) -> FakeApi:
    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            api = api_holder[0]
            length = int(self.headers.get("content-length") or 0)
            api.requests.append(
                Recorded(
                    method=self.command,
                    path=self.path,
                    headers={k.lower(): v for k, v in self.headers.items()},
                    body=self.rfile.read(length) if length else b"",
                )
            )
            payload = api.body if isinstance(api.body, bytes) else json.dumps(api.body).encode()
            self.send_response(api.status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            for key, value in api.headers.items():
                self.send_header(key, value)
            self.end_headers()
            with contextlib.suppress(BrokenPipeError):  # the client gave up (timeout tests)
                self.wfile.write(payload)

        do_GET = _handle
        do_POST = _handle

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api = FakeApi(server=server)
    api_holder.append(api)
    return api


def _fresh(api: FakeApi) -> FakeApi:
    api.requests.clear()
    api.status, api.body, api.headers = 200, {"ok": True}, {}
    return api


@pytest.fixture(scope="session")
def _servers() -> Iterator[tuple[FakeApi, FakeApi]]:
    apis = (_start([]), _start([]))
    yield apis
    for api in apis:
        api.server.shutdown()


@pytest.fixture
def producer(_servers: tuple[FakeApi, FakeApi]) -> FakeApi:
    return _fresh(_servers[0])


@pytest.fixture
def control(_servers: tuple[FakeApi, FakeApi]) -> FakeApi:
    return _fresh(_servers[1])


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, producer: FakeApi, control: FakeApi) -> None:
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDTESTING")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session-token")
    monkeypatch.setenv("POWERTOOLS_SERVICE_NAME", "ui-bff")
    monkeypatch.setenv("PRODUCER_API_URL", producer.url)
    monkeypatch.setenv("CONTROL_API_URL", control.url)
    from bff import config, upstream

    upstream._session.cache_clear()
    config.get_settings.cache_clear()


@pytest.fixture
def setenv(monkeypatch: pytest.MonkeyPatch) -> SetEnv:
    """Change the function's environment mid-test (the settings are read once, then cached)."""
    from bff import config

    def _set(**values: str | None) -> None:
        for name, value in values.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        config.get_settings.cache_clear()

    return _set


def closed_port_url() -> str:
    """A URL nothing listens on (connection refused)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/prod"


class Context:
    function_name = "ui-test-bff"
    memory_limit_in_mb = 256
    invoked_function_arn = f"arn:aws:lambda:{REGION}:123456789012:function:ui-test-bff"
    aws_request_id = "req-1"


def make_event(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    query: str = "",
    body: str | bytes | None = None,
    base64_body: bool = False,
) -> dict[str, Any]:
    """A Lambda function URL event (payload format 2.0), as CloudFront's OAC forwards it."""
    raw_body: str | None
    if isinstance(body, bytes) or base64_body:
        data = body if isinstance(body, bytes) else (body or "").encode()
        raw_body = base64.b64encode(data).decode()
        base64_body = True
    else:
        raw_body = body
    event: dict[str, Any] = {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": query,
        "headers": {k.lower(): v for k, v in (headers or {}).items()},
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "fnurl",
            "domainName": f"fnurl.lambda-url.{REGION}.on.aws",
            "domainPrefix": "fnurl",
            "http": {
                "method": method,
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "test",
            },
            "requestId": "req-1",
            "routeKey": "$default",
            "stage": "$default",
            "time": "11/Oct/2026:00:00:00 +0000",
            "timeEpoch": 1791676800000,
        },
        "isBase64Encoded": base64_body,
    }
    if query:
        event["queryStringParameters"] = dict(p.split("=", 1) for p in query.split("&"))
    if raw_body is not None:
        event["body"] = raw_body
    return event


Invoke = Callable[..., dict[str, Any]]
SetEnv = Callable[..., None]


@pytest.fixture
def invoke() -> Invoke:
    from bff.app import handler

    def _invoke(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        return handler(make_event(method, path, **kwargs), Context())  # type: ignore[arg-type]

    return _invoke


UI_HEADERS = {"X-Requested-With": "entingestion-ui", "Sec-Fetch-Site": "same-origin"}
