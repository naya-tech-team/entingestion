"""End-to-end check of the deployed upload UI (docs/UI.md), with 3 small sample files.

    cd services/ui/bff
    UI_URL=https://dxxxx.cloudfront.net UI_USER=... UI_PASSWORD=... \
    AWS_REGION=us-west-2 uv run python scripts/ui_acceptance.py

It drives the UI's own path (CloudFront -> BFF -> APIs, plus the browser's presigned PUT):
the login gate, the direct-origin denials, the security headers, the CSRF guard and route
allow-list, then one batch through to INDEXED. The articles are deleted afterwards through
the producer API, signed with your AWS credentials (the BFF can't delete).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

import botocore.session
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

SOURCE = os.environ.get("UI_SOURCE", "acceptance")
SAMPLES = {
    "ui-sample-1.md": ("text/markdown", "# Tide pools\n\nTide pools hold anemones and crabs.\n"),
    "ui-sample-2.txt": ("text/plain", "Lighthouses guided ships along rocky coasts for centuries."),
    "ui-sample-3.html": (
        "text/html",
        "<html><head><title>Kelp</title></head><body><h1>Kelp forests</h1>"
        "<p>Giant kelp grows up to half a metre a day.</p></body></html>",
    ),
}
FINAL = {"COMPLETE", "COMPLETE_WITH_ERRORS"}

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("PASS " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def http(
    method: str, url: str, *, headers: dict[str, str] | None = None, body: bytes | None = None
) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()


class Ui:
    def __init__(self, base: str, user: str, password: str) -> None:
        self.base = base.rstrip("/")
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.auth = {"Authorization": f"Basic {token}"}

    def get(self, path: str) -> tuple[int, Any]:
        status, _, body = http("GET", self.base + path, headers=self.auth)
        return status, _json(body)

    def post(self, path: str, payload: Any, *, csrf: bool = True) -> tuple[int, Any]:
        body = json.dumps(payload).encode()
        headers = {
            **self.auth,
            "Content-Type": "application/json",
            "x-amz-content-sha256": hashlib.sha256(body).hexdigest(),  # Lambda OAC
            "Idempotency-Key": str(uuid.uuid4()),
        }
        if csrf:
            headers["X-Requested-With"] = "entingestion-ui"
        status, _, raw = http("POST", self.base + path, headers=headers, body=body)
        return status, _json(raw)


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw or b"null")
    except ValueError:
        return raw.decode(errors="replace")


def ssm(name: str) -> str:
    client = botocore.session.get_session().create_client("ssm")
    return str(client.get_parameter(Name=name)["Parameter"]["Value"])


def signed_delete(url: str) -> int:
    session = botocore.session.get_session()
    creds = session.get_credentials()
    assert creds is not None, "no AWS credentials for the cleanup"
    region = session.get_config_variable("region") or os.environ["AWS_REGION"]
    request = AWSRequest(method="DELETE", url=url, headers={"host": url.split("/")[2]})
    SigV4Auth(creds.get_frozen_credentials(), "execute-api", region).add_auth(request)
    status, _, _ = http("DELETE", url, headers=dict(request.headers.items()))
    return status


def gate(ui: Ui, env: str) -> None:
    status, _, _ = http("GET", ui.base + "/")
    check(status == 401, f"no credentials -> 401 (got {status})")
    wrong = base64.b64encode(b"nobody:wrong").decode()
    status, _, _ = http("GET", ui.base + "/", headers={"Authorization": f"Basic {wrong}"})
    check(status == 401, f"wrong credentials -> 401 (got {status})")
    status, _, _ = http(
        "GET", ui.base + "/api/sources", headers={"Authorization": f"Basic {wrong}"}
    )
    check(status == 401, f"wrong credentials on /api -> 401 (got {status})")

    lam = botocore.session.get_session().create_client("lambda")
    fn_url = lam.get_function_url_config(FunctionName=f"ui-{env}-bff", Qualifier="live")
    status, _, _ = http("GET", fn_url["FunctionUrl"].rstrip("/") + "/api/sources")
    check(status == 403, f"direct function URL -> 403 (got {status})")

    status, headers, body = http("GET", ui.base + "/", headers=ui.auth)
    check(
        status == 200 and b"<div id=" in body, f"index.html with credentials -> 200 (got {status})"
    )
    csp = headers.get("content-security-policy", "")
    check("frame-ancestors 'none'" in csp and "connect-src 'self'" in csp, "CSP header present")
    check("strict-transport-security" in headers, "HSTS header present")
    check(headers.get("x-content-type-options") == "nosniff", "nosniff header present")


def guards(ui: Ui) -> None:
    status, _ = ui.post("/api/processes", {"name": "csrf", "source": SOURCE}, csrf=False)
    check(status == 403, f"POST without X-Requested-With -> 403 (got {status})")
    status, _, _ = http("DELETE", ui.base + "/api/articles/art_x", headers=ui.auth)
    check(status in {403, 404, 405}, f"DELETE not routed -> 403/404/405 (got {status})")
    status, _ = ui.get("/api/tables")
    check(status == 404, f"unlisted route -> 404 (got {status})")


def batch(ui: Ui) -> list[str]:
    status, sources = ui.get("/api/sources")
    writable = {s["source"] for s in (sources or {}).get("items", []) if s["access"] == "write"}
    check(status == 200 and SOURCE in writable, f"/api/sources lists {SOURCE} as writable")

    status, proc = ui.post("/api/processes", {"name": "ui acceptance", "source": SOURCE})
    check(status == 201, f"create process -> 201 (got {status}: {proc})")
    if status != 201:
        return []
    pid = proc["process_id"]
    print(f"     process {pid}")

    articles = []
    for name, (ctype, text) in SAMPLES.items():
        data = (text + f"\n\nrun {uuid.uuid4()}\n").encode()
        status, ticket = ui.post(
            f"/api/processes/{pid}/uploads",
            {
                "filename": name,
                "content_type": ctype,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "external_id": f"ui-acceptance-{name}",
            },
        )
        check(status == 201, f"ticket for {name} -> 201 (got {status})")
        if status != 201:
            continue
        articles.append(ticket["article_id"])
        # What the browser does: a CORS preflight, then the PUT with the ticket's headers.
        pre, pre_headers, _ = http(
            "OPTIONS",
            ticket["url"],
            headers={
                "Origin": ui.base,
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": ",".join(h.lower() for h in ticket["headers"]),
            },
        )
        check(
            pre == 200 and pre_headers.get("access-control-allow-origin") == ui.base,
            f"S3 CORS preflight for {name} allows the UI origin (got {pre})",
        )
        put, _, _ = http(
            "PUT", ticket["url"], headers={**ticket["headers"], "Origin": ui.base}, body=data
        )
        check(put == 200, f"presigned PUT {name} -> 200 (got {put})")

    status, _ = ui.post(f"/api/processes/{pid}/close", {"expected_count": len(articles)})
    check(status == 200, f"close -> 200 (got {status})")

    deadline = time.monotonic() + 600
    summary: Any = None
    while time.monotonic() < deadline:
        status, summary = ui.get(f"/api/processes/{pid}/summary")
        if status == 200 and summary.get("status") in FINAL:
            break
        time.sleep(10)
    check(
        summary is not None and summary.get("status") == "COMPLETE", f"summary COMPLETE: {summary}"
    )

    status, listed = ui.get(f"/api/processes/{pid}/articles")
    statuses = sorted(i["status"] for i in (listed or {}).get("items", []))
    check(statuses == ["INDEXED"] * len(articles), f"process articles all INDEXED: {statuses}")

    status, uploads = ui.get(f"/api/processes/{pid}/uploads")
    states = sorted(u["status"] for u in (uploads or {}).get("items", []))
    check(states == ["REGISTERED"] * len(articles), f"uploads all REGISTERED: {states}")

    if articles:
        status, detail = ui.get(f"/api/articles/{articles[0]}/status")
        steps = [s["step"] for s in (detail or {}).get("steps", [])]
        check(status == 200 and "FINALIZE" in steps, f"article status has steps: {steps}")
    return articles


def cleanup(env: str, articles: list[str]) -> None:
    api = ssm(f"/entingestion/{env}/producer/api_url")
    for aid in articles:
        status = signed_delete(f"{api}/v1/articles/{aid}")
        check(status == 204, f"cleanup delete {aid} -> 204 (got {status})")


def main() -> int:
    env = os.environ.get("UI_ENV", "dev")
    ui = Ui(os.environ["UI_URL"], os.environ["UI_USER"], os.environ["UI_PASSWORD"])
    gate(ui, env)
    guards(ui)
    articles = batch(ui)
    cleanup(env, articles)
    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
