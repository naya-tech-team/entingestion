from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.conftest import UI_HEADERS, FakeApi, Invoke, closed_port_url

PID = "prc_01J9ZK3Q8V6Y2B7N4M5X0W1T2R"
AID = "art_0123456789abcdef0123456789abcdef"


def _body(response: dict[str, Any]) -> Any:
    return json.loads(response["body"])


# -- Route allow-list ---------------------------------------------------------------------------

ROUTES = [
    # (method, BFF path, upstream, upstream path)
    ("GET", "/api/sources", "producer", "/prod/v1/sources"),
    ("GET", "/api/processes", "producer", "/prod/v1/processes"),
    ("POST", "/api/processes", "producer", "/prod/v1/processes"),
    ("GET", f"/api/processes/{PID}", "producer", f"/prod/v1/processes/{PID}"),
    ("POST", f"/api/processes/{PID}/close", "producer", f"/prod/v1/processes/{PID}/close"),
    ("POST", f"/api/processes/{PID}/uploads", "producer", f"/prod/v1/processes/{PID}/uploads"),
    ("GET", f"/api/processes/{PID}/uploads", "producer", f"/prod/v1/processes/{PID}/uploads"),
    ("GET", f"/api/processes/{PID}/summary", "control", f"/prod/v1/processes/{PID}/summary"),
    ("GET", f"/api/processes/{PID}/articles", "control", f"/prod/v1/processes/{PID}/articles"),
    ("GET", f"/api/articles/{AID}/status", "control", f"/prod/v1/articles/{AID}/status"),
]


@pytest.mark.parametrize(("method", "path", "api", "upstream_path"), ROUTES)
def test_each_route_reaches_its_upstream(
    invoke: Invoke,
    producer: FakeApi,
    control: FakeApi,
    method: str,
    path: str,
    api: str,
    upstream_path: str,
) -> None:
    target, other = (producer, control) if api == "producer" else (control, producer)
    response = invoke(method, path, headers=UI_HEADERS, body="{}" if method == "POST" else None)

    assert response["statusCode"] == 200
    assert _body(response) == {"ok": True}
    assert response["headers"]["Content-Type"] == "application/json"
    assert (target.last.method, target.last.path) == (method, upstream_path)
    assert other.requests == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/tables"),
        ("GET", "/api/articles/x"),
        ("DELETE", f"/api/articles/{AID}"),
        ("PATCH", f"/api/articles/{AID}"),
        ("POST", f"/api/articles/{AID}/reindex"),
        ("DELETE", f"/api/processes/{PID}"),
        ("PUT", "/api/processes"),
        ("POST", "/api/sources"),
        ("POST", f"/api/processes/{PID}"),
        ("POST", f"/api/processes/{PID}/summary"),
        ("GET", f"/api/processes/{PID}/close"),
        ("GET", "/v1/processes"),
        ("GET", "/api/processes/a/b/uploads"),
        ("GET", "/"),
    ],
)
def test_anything_else_is_404(
    invoke: Invoke, producer: FakeApi, control: FakeApi, method: str, path: str
) -> None:
    response = invoke(method, path, headers=UI_HEADERS, body="{}")

    assert response["statusCode"] == 404
    assert _body(response) == {"message": "route not found"}
    assert producer.requests == control.requests == []


# -- Path ids ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    ["a.b", "a%2Fb", "..", "a" * 129, "prc%20x", "a:b", "x'y", "a=b"],
)
@pytest.mark.parametrize(
    ("method", "template"),
    [
        ("GET", "/api/processes/{}"),
        ("GET", "/api/processes/{}/uploads"),
        ("POST", "/api/processes/{}/uploads"),
        ("POST", "/api/processes/{}/close"),
        ("GET", "/api/processes/{}/summary"),
        ("GET", "/api/articles/{}/status"),
    ],
)
def test_ids_are_validated(
    invoke: Invoke, producer: FakeApi, control: FakeApi, method: str, template: str, bad_id: str
) -> None:
    response = invoke(method, template.format(bad_id), headers=UI_HEADERS, body="{}")

    assert response["statusCode"] == 400
    assert _body(response) == {"message": "invalid id"}
    assert producer.requests == control.requests == []


def test_longest_valid_id_is_accepted(invoke: Invoke, control: FakeApi) -> None:
    response = invoke("GET", f"/api/articles/{'A-_9' * 32}/status")

    assert response["statusCode"] == 200
    assert control.last.path == f"/prod/v1/articles/{'A-_9' * 32}/status"


# -- CSRF guard --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        ({"X-Requested-With": "entingestion-ui", "Sec-Fetch-Site": "same-origin"}, True),
        ({"X-Requested-With": "entingestion-ui"}, True),  # scripts send no Sec-Fetch-Site
        ({}, False),
        ({"Sec-Fetch-Site": "same-origin"}, False),
        ({"X-Requested-With": "XMLHttpRequest", "Sec-Fetch-Site": "same-origin"}, False),
        ({"X-Requested-With": "entingestion-ui", "Sec-Fetch-Site": "cross-site"}, False),
        ({"X-Requested-With": "entingestion-ui", "Sec-Fetch-Site": "same-site"}, False),
        ({"X-Requested-With": "entingestion-ui", "Sec-Fetch-Site": "none"}, False),
    ],
)
@pytest.mark.parametrize(
    "path", ["/api/processes", f"/api/processes/{PID}/close", f"/api/processes/{PID}/uploads"]
)
def test_csrf_guard_on_posts(
    invoke: Invoke, producer: FakeApi, headers: dict[str, str], allowed: bool, path: str
) -> None:
    response = invoke("POST", path, headers=headers, body="{}")

    if allowed:
        assert response["statusCode"] == 200
        assert len(producer.requests) == 1
    else:
        assert response["statusCode"] == 403
        assert _body(response) == {"message": "forbidden"}
        assert producer.requests == []


def test_csrf_is_checked_before_the_id(invoke: Invoke) -> None:
    response = invoke("POST", "/api/processes/a.b/close", body="{}")

    assert response["statusCode"] == 403


def test_gets_need_no_csrf_header(invoke: Invoke, producer: FakeApi) -> None:
    response = invoke("GET", "/api/sources", headers={"Sec-Fetch-Site": "cross-site"})

    assert response["statusCode"] == 200


# -- Signing -----------------------------------------------------------------------------------


def test_requests_are_sigv4_signed_for_execute_api(invoke: Invoke, producer: FakeApi) -> None:
    invoke("POST", "/api/processes", headers=UI_HEADERS, body='{"name":"n","source":"bruno"}')

    headers = producer.last.headers
    today = datetime.now(UTC).strftime("%Y%m%d")
    assert headers["authorization"].startswith(
        f"AWS4-HMAC-SHA256 Credential=AKIDTESTING/{today}/us-west-2/execute-api/aws4_request"
    )
    signed = re.search(r"SignedHeaders=([^,]+)", headers["authorization"])
    assert signed is not None
    assert {"host", "x-amz-date", "x-amz-security-token", "content-type"} <= set(
        signed.group(1).split(";")
    )
    assert re.fullmatch(r"\d{8}T\d{6}Z", headers["x-amz-date"])
    assert headers["x-amz-security-token"] == "session-token"
    assert headers["host"] == producer.url.split("/")[2]
    assert headers["content-type"] == "application/json"


def test_viewer_credentials_and_headers_are_not_forwarded(
    invoke: Invoke, producer: FakeApi
) -> None:
    invoke(
        "GET",
        "/api/sources",
        headers={"Authorization": "Basic dTpw", "Cookie": "a=b", "X-Forwarded-For": "1.2.3.4"},
    )

    headers = producer.last.headers
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert "cookie" not in headers
    assert "x-forwarded-for" not in headers


def test_region_comes_from_the_execute_api_host() -> None:
    from bff.upstream import region_of

    assert region_of("https://abc.execute-api.eu-west-1.amazonaws.com/prod") == "eu-west-1"
    assert region_of("http://127.0.0.1:1/prod") == "us-west-2"  # AWS_REGION


# -- Bodies, query strings, idempotency --------------------------------------------------------


def test_post_body_is_forwarded_byte_for_byte(invoke: Invoke, producer: FakeApi) -> None:
    body = '{"filename":"r\\u00e9sum\\u00e9.pdf","size_bytes":3}'
    invoke("POST", f"/api/processes/{PID}/uploads", headers=UI_HEADERS, body=body)

    assert producer.last.body == body.encode()


def test_base64_bodies_are_decoded(invoke: Invoke, producer: FakeApi) -> None:
    body = json.dumps({"name": "café"}, ensure_ascii=False).encode()
    invoke("POST", "/api/processes", headers=UI_HEADERS, body=body)

    assert producer.last.body == body


def test_idempotency_key_is_forwarded_on_posts(invoke: Invoke, producer: FakeApi) -> None:
    invoke("POST", "/api/processes", headers={**UI_HEADERS, "Idempotency-Key": "k-123"}, body="{}")
    assert producer.last.headers["idempotency-key"] == "k-123"

    invoke("POST", "/api/processes", headers=UI_HEADERS, body="{}")
    assert "idempotency-key" not in producer.last.headers


@pytest.mark.parametrize(
    ("path", "query", "upstream_path"),
    [
        (
            "/api/processes",
            "source=bruno&limit=20&next_token=eyJhIjoiYiJ9",
            "/prod/v1/processes?source=bruno&limit=20&next_token=eyJhIjoiYiJ9",
        ),
        (
            f"/api/processes/{PID}/uploads",
            "limit=500&next_token=a%2Bb%2Fc%3D",
            f"/prod/v1/processes/{PID}/uploads?limit=500&next_token=a%2Bb%2Fc%3D",
        ),
        ("/api/processes", "source=a+b", "/prod/v1/processes?source=a%20b"),
    ],
)
def test_query_is_passed_through_on_list_routes(
    invoke: Invoke, producer: FakeApi, path: str, query: str, upstream_path: str
) -> None:
    invoke("GET", path, query=query)

    assert producer.last.path == upstream_path


def test_query_is_dropped_elsewhere(invoke: Invoke, producer: FakeApi) -> None:
    invoke("GET", f"/api/processes/{PID}", query="x=1")

    assert producer.last.path == f"/prod/v1/processes/{PID}"


# -- Upstream responses ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (201, {"process_id": PID, "status": "OPEN"}),
        (403, {"message": "not allowed to write source news"}),
        (404, {"message": "process not found"}),
        (409, {"message": "process is not OPEN"}),
        (422, {"message": "Request validation failed", "errors": [{"loc": ["body"], "msg": "x"}]}),
        (500, {"message": "Internal server error"}),
    ],
)
def test_upstream_status_and_body_pass_through(
    invoke: Invoke, producer: FakeApi, status: int, body: dict[str, Any]
) -> None:
    producer.status, producer.body = status, body
    response = invoke("POST", "/api/processes", headers=UI_HEADERS, body="{}")

    assert response["statusCode"] == status
    assert _body(response) == body
    assert response["headers"]["Content-Type"] == "application/json"
    assert response["headers"]["Cache-Control"] == "no-store"


def test_throttling_keeps_retry_after(invoke: Invoke, control: FakeApi) -> None:
    control.status, control.body = 429, {"message": "Too Many Requests"}
    control.headers = {"Retry-After": "2"}
    response = invoke("GET", f"/api/processes/{PID}/summary")

    assert response["statusCode"] == 429
    assert response["headers"]["Retry-After"] == "2"


def test_connection_error_is_502(invoke: Invoke, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTROL_API_URL", closed_port_url())
    response = invoke("GET", f"/api/articles/{AID}/status")

    assert response["statusCode"] == 502
    assert _body(response) == {"message": "upstream unavailable"}


def test_timeout_is_502(invoke: Invoke, producer: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    from bff import upstream

    original = producer.server.RequestHandlerClass.do_GET  # type: ignore[attr-defined]

    def slow(self: Any) -> None:
        time.sleep(0.5)
        original(self)

    monkeypatch.setattr(producer.server.RequestHandlerClass, "do_GET", slow)
    monkeypatch.setattr(upstream, "READ_TIMEOUT", 0.1)
    response = invoke("GET", "/api/sources")

    assert response["statusCode"] == 502


def test_missing_configuration_is_a_500(invoke: Invoke, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRODUCER_API_URL")
    response = invoke("GET", "/api/sources")

    assert response["statusCode"] == 500
    assert _body(response) == {"message": "internal server error"}


def test_bodies_are_not_logged(
    invoke: Invoke, producer: FakeApi, caplog: pytest.LogCaptureFixture
) -> None:
    producer.body = {"secret": "upstream-body-marker"}
    with caplog.at_level("INFO"):
        invoke("POST", "/api/processes", headers=UI_HEADERS, body='{"name":"request-body-marker"}')

    forwarded = [r for r in caplog.records if r.getMessage() == "forwarded"]
    assert len(forwarded) == 1
    assert forwarded[0].__dict__["status"] == 200
    assert "body-marker" not in str([r.__dict__ for r in caplog.records])
