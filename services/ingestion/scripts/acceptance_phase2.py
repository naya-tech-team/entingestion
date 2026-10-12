"""Phase 2 gate against a deployed environment (brief §15: UC1, UC3-UC8, UC11, DUP, CONC).

    uv run python scripts/acceptance_phase2.py [--env dev] [--only api,uc3] [--skip uc6]

Scenarios run through the real producer (producer-cli and its API), the control API and the
reconciler Lambda. uc6 waits for a lease to expire (LEASE_SECONDS, 15 minutes), so it runs last.

UC4 (Bedrock throttled once -> same step item, attempt 2) can't be forced on demand against the
real service; it is covered by the unit tests (test_status_store: retry updates the same item).
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import boto3
from acceptance import SOURCE, Env, check, conc, dup, plain
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from gate_phase1 import ROOT, tf_output
from pypdf import PdfReader, PdfWriter

sys.path.insert(0, str(ROOT / "services" / "ingestion"))
from tests.unit.test_extract_normalize_chunk import pdf_bytes

PRODUCER = ROOT / "services" / "producer"
MAPPING_DRAIN_S = 120


# -- plumbing -------------------------------------------------------------------------------


class Signed:
    """SigV4-signed JSON calls to an API Gateway (execute-api) API."""

    def __init__(self, base_url: str, region: str) -> None:
        self.base = base_url.rstrip("/")
        self.region = region
        self.session = boto3.Session()

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        sign: bool = True,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {**({"Content-Type": "application/json"} if data is not None else {}),
                   **(headers or {})}  # fmt: skip
        if sign:
            creds = self.session.get_credentials()
            if creds is None:
                raise RuntimeError("no AWS credentials")
            request = AWSRequest(method=method, url=url, data=data or b"", headers=headers)
            SigV4Auth(creds.get_frozen_credentials(), "execute-api", self.region).add_auth(request)
            headers = dict(request.headers.items())
        req = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return err.code, json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return err.code, raw.decode(errors="replace")


class Ctx:
    def __init__(self, env_name: str) -> None:
        self.env = Env(env_name)
        region = boto3.Session().region_name or "us-west-2"
        self.control = Signed(tf_output("ingestion", env_name, "control_api_url"), region)
        self.producer_api = Signed(self.env.api_url, region)
        self.reconciler = tf_output("ingestion", env_name, "reconciler_function_name")
        self.lam = boto3.client("lambda")
        self.failures: list[str] = []
        self.state: dict[str, Any] = {}

    def check(self, ok: bool, label: str, scenario: str) -> None:
        check(self.failures, ok, label)
        if not ok:
            self.failures[-1] = f"{scenario}: {label}"

    # producer
    def cli(self, *args: str, cwd: Path) -> dict[str, Any]:
        run = subprocess.run(
            ["uv", "run", "--project", str(PRODUCER), "producer-cli", "--api-url",
             self.env.api_url, *args],
            cwd=cwd, capture_output=True, text=True, check=False,
        )  # fmt: skip
        if run.returncode != 0:
            raise RuntimeError(f"producer-cli {args[0]} failed: {run.stderr[-1500:]}")
        return dict(json.loads(run.stdout))

    def ingest_files(self, files: dict[str, bytes], name: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            for filename, data in files.items():
                (Path(tmp) / filename).write_bytes(data)
            return self.cli("ingest-dir", tmp, "--name", name, "--source", SOURCE, "--close",
                            cwd=Path(tmp))  # fmt: skip

    def upload(self, process_id: str, filename: str, data: bytes) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / filename).write_bytes(data)
            out = self.cli("upload", process_id, filename, "--external-id", "filename",
                           cwd=Path(tmp))  # fmt: skip
            return dict(out["uploaded"][0])

    # ingestion
    def latest(self, article_id: str) -> dict[str, Any]:
        return self.env.item(article_id, "LATEST")

    def summary(self, process_id: str) -> dict[str, Any]:
        return self.env.item(f"PROCESS#{process_id}", "SUMMARY")

    def counts(self, process_id: str) -> dict[str, int]:
        s = self.summary(process_id)
        return {k: int(s.get(k, 0)) for k in ("indexed", "skipped", "failed")}

    def steps(self, article_id: str, run_id: str) -> list[dict[str, Any]]:
        resp = self.env.ddb.query(
            TableName=self.env.status_table,
            KeyConditionExpression="pk = :p AND begins_with(sk, :r)",
            ExpressionAttributeValues={":p": {"S": article_id}, ":r": {"S": f"RUN#{run_id}#"}},
            ConsistentRead=True,
        )
        return [plain(i) for i in resp["Items"]]

    def reconcile(self) -> dict[str, Any]:
        resp = self.lam.invoke(FunctionName=self.reconciler)
        out = json.loads(resp["Payload"].read())
        if resp.get("FunctionError"):
            raise RuntimeError(f"reconciler failed: {out}")
        return dict(out)


def wait[T](fn: Callable[[], T | None], label: str, timeout: float = 300, every: float = 3) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(every)
    raise TimeoutError(f"timed out waiting for {label}")


def article_text(tag: str, paragraphs: int = 8) -> bytes:
    body = "\n\n".join(
        f"Paragraph {i} of the {tag} acceptance article. "
        + " ".join(f"It covers point {i}.{j} in some detail." for j in range(25))
        for i in range(paragraphs)
    )
    return f"Acceptance {tag}\n\n{body}\n\nNonce {uuid.uuid4()}.\n".encode()


def encrypted(pdf: bytes) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    writer.encrypt(user_password="pw", owner_password="pw", algorithm="AES-256")  # noqa: S106
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def in_status(ctx: Ctx, article_id: str, status: str) -> dict[str, Any] | None:
    latest = ctx.latest(article_id)
    return latest if latest.get("status") == status else None


def settled(ctx: Ctx, pid: str) -> dict[str, Any] | None:
    s = ctx.summary(pid)
    return s if s.get("status") in ("COMPLETE", "COMPLETE_WITH_ERRORS") else None


# -- scenarios ------------------------------------------------------------------------------


def dup_conc_uc1_uc11(ctx: Ctx) -> None:
    result = dup(ctx.env)
    ctx.failures += [f"DUP: {f}" for f in result.failures]
    ctx.state["dup"] = result
    a, pid = result.article_id, result.process_id

    print("UC1: new article indexed with 10 records")
    latest = ctx.latest(a)
    [run] = [r for r in ctx.env.runs(a) if r.get("process_id") == pid]
    items = ctx.steps(a, run["run_id"])
    ctx.check(latest.get("status") == "INDEXED" and int(latest.get("chunk_count", 0)) > 0,
              f"LATEST INDEXED with chunks ({latest.get('chunk_count')})", "UC1")  # fmt: skip
    ctx.check(len(items) + 2 == 10, f"RUN# + {len(items)} steps + LATEST = 10", "UC1")

    print("UC11: process settles -> SUMMARY COMPLETE, completion recorded")
    finalize = next((i for i in items if str(i["sk"]).endswith("FINALIZE")), {})
    ctx.check(ctx.summary(pid).get("status") == "COMPLETE", "SUMMARY COMPLETE", "UC11")
    ctx.check(bool((finalize.get("metrics") or {}).get("process_completed")),
              "Finalize completed the process (process.ingested emitted)", "UC11")  # fmt: skip

    ctx.failures += [f"CONC: {f}" for f in conc(ctx.env, a, pid)]


def api(ctx: Ctx) -> None:
    print("API: control API auth, reads and validation")
    a, pid = ctx.state["dup"].article_id, ctx.state["dup"].process_id
    status, _ = ctx.control.call("GET", f"/v1/articles/{a}/status", sign=False)
    ctx.check(status == 403, f"unsigned call refused ({status})", "API")
    status, out = ctx.control.call("GET", f"/v1/articles/{a}/status")
    ctx.check(status == 200 and out["latest"]["status"] == "INDEXED", f"article status {status}",
              "API")  # fmt: skip
    ctx.check(status == 200 and bool(out.get("execution")), "execution link present", "API")
    status, out = ctx.control.call("GET", f"/v1/processes/{pid}/summary")
    ctx.check(status == 200 and out["counts"]["indexed"] == 1, f"process summary {status}", "API")
    status, _ = ctx.control.call("POST", "/v1/ingest", {"article_ids": []})
    ctx.check(status == 422, f"bad request rejected ({status})", "API")
    status, out = ctx.control.call("POST", "/v1/ingest", {"article_ids": [a], "dry_run": True})
    ctx.check(status == 200 and out["count"] == 1, f"dry run {status}", "API")
    counts = ctx.reconcile()
    ctx.check(isinstance(counts, dict), f"reconciler runs: {counts}", "API")


def uc3(ctx: Ctx) -> None:
    print("UC3: unchanged re-registration in a new process, then a metadata PATCH")
    d = ctx.state["dup"]
    result = ctx.ingest_files({d.filename: d.text.encode()}, "acceptance UC3")
    pid = result["process_id"]
    ctx.check(result["uploaded"][0]["article_id"] == d.article_id, "same article", "UC3")
    wait(lambda: settled(ctx, pid), "UC3 process")
    runs = [r for r in ctx.env.runs(d.article_id) if r.get("process_id") == pid]
    ctx.check(len(runs) == 1 and runs[0].get("outcome") == "SKIPPED_UNCHANGED",
              f"one SKIPPED_UNCHANGED run: {[r.get('outcome') for r in runs]}", "UC3")  # fmt: skip
    ctx.check(runs != [] and ctx.steps(d.article_id, runs[0]["run_id"]) == [],
              "1 record (no step items: no Bedrock or DB work)", "UC3")  # fmt: skip
    ctx.check(ctx.counts(pid) == {"indexed": 0, "skipped": 1, "failed": 0},
              f"counted as skipped: {ctx.counts(pid)}", "UC3")  # fmt: skip

    before = len(ctx.env.runs(d.article_id))
    status, _ = ctx.producer_api.call("PATCH", f"/v1/articles/{d.article_id}",
                                      {"metadata": {"acceptance": "uc3"}})  # fmt: skip
    ctx.check(status == 200, f"PATCH accepted ({status})", "UC3")
    wait(lambda: len(ctx.env.runs(d.article_id)) > before, "PATCH run", timeout=180)
    newest = sorted(ctx.env.runs(d.article_id), key=lambda r: str(r["run_id"]))[-1]
    ctx.check(newest.get("outcome") == "SKIPPED_UNCHANGED", f"PATCH run {newest.get('outcome')}",
              "UC3")  # fmt: skip
    ctx.check(ctx.counts(pid) == {"indexed": 0, "skipped": 1, "failed": 0},
              f"PATCH doesn't change the process counts: {ctx.counts(pid)}", "UC3")  # fmt: skip


def uc5(ctx: Ctx) -> None:
    print("UC5: encrypted PDF fails permanently; redrive by error_code; fixed upload -> indexed")
    filename = f"uc5-{uuid.uuid4().hex[:8]}.pdf"
    good = pdf_bytes(3)
    with tempfile.TemporaryDirectory() as tmp:
        pid = ctx.cli("create-process", "--name", "acceptance UC5", "--source", SOURCE,
                      cwd=Path(tmp))["process_id"]  # fmt: skip
    a = ctx.upload(pid, filename, encrypted(good))["article_id"]
    latest = wait(lambda: in_status(ctx, a, "FAILED_PERMANENT"), "UC5 failure")
    ctx.check(latest.get("error_code") == "E_UNREADABLE", f"error {latest.get('error_code')}",
              "UC5")  # fmt: skip
    ctx.check(ctx.counts(pid)["failed"] == 1, f"failed counted: {ctx.counts(pid)}", "UC5")

    runs_before = len(ctx.env.runs(a))
    status, out = ctx.control.call("POST", "/v1/dlq/redrive",
                                   {"error_code": "E_UNREADABLE", "max": 20})  # fmt: skip
    ctx.check(status == 202 and out["queued"] >= 1, f"redrive queued {out.get('queued')}", "UC5")
    wait(lambda: len(ctx.env.runs(a)) > runs_before
         and ctx.latest(a).get("status") == "FAILED_PERMANENT"
         and ctx.latest(a).get("run_id") != latest.get("run_id"), "redriven run")  # fmt: skip
    redriven = ctx.latest(a)
    first = sorted(str(i["sk"]) for i in ctx.steps(a, str(redriven["run_id"])))
    ctx.check(bool(first) and first[0].endswith("EXTRACT"),
              f"redrive resumed after Fetch: {first[:1]}", "UC5")  # fmt: skip
    ctx.check(ctx.counts(pid)["failed"] == 1, "still counted once while unfixed", "UC5")

    ctx.upload(pid, filename, good)  # the fix: a readable version of the same article
    with tempfile.TemporaryDirectory() as tmp:
        ctx.cli("close", pid, "--expected-count", "1", cwd=Path(tmp))
    wait(lambda: ctx.latest(a).get("status") == "INDEXED", "UC5 fixed version", timeout=300)
    summary = wait(lambda: settled(ctx, pid), "UC5 process")
    ctx.check(ctx.counts(pid) == {"indexed": 1, "skipped": 0, "failed": 0},
              f"failed -1, indexed +1: {ctx.counts(pid)}", "UC5")  # fmt: skip
    ctx.check(summary.get("status") == "COMPLETE", f"process {summary.get('status')}", "UC5")


def _ingest_mapping(ctx: Ctx) -> str:
    queue_arn = ctx.env.sqs.get_queue_attributes(
        QueueUrl=ctx.env.ingest_queue, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]
    mappings = ctx.lam.list_event_source_mappings(
        FunctionName=f"ingestion-{ctx.env.env}-dispatcher", EventSourceArn=queue_arn
    )["EventSourceMappings"]
    return str(mappings[0]["UUID"])


def _set_mapping(ctx: Ctx, uuid_: str, enabled: bool) -> None:
    ctx.lam.update_event_source_mapping(UUID=uuid_, Enabled=enabled)
    want = "Enabled" if enabled else "Disabled"
    wait(lambda: ctx.lam.get_event_source_mapping(UUID=uuid_)["State"] == want,
         f"mapping {want}", timeout=180, every=5)  # fmt: skip
    if not enabled:
        # "Disabled" is reported before the pollers stop; give in-flight polls time to drain.
        time.sleep(MAPPING_DRAIN_S)


def uc7(ctx: Ctx) -> None:
    print("UC7: stream event dropped -> reconciler enqueues from gsi_updated")
    mapping = _ingest_mapping(ctx)
    _set_mapping(ctx, mapping, enabled=False)
    try:
        result = ctx.ingest_files({f"uc7-{uuid.uuid4().hex[:8]}.txt": article_text("UC7")},
                                  "acceptance UC7")  # fmt: skip
        pid, a = result["process_id"], result["uploaded"][0]["article_id"]

        def drop() -> bool:  # take our stream record off the queue: "dropped"
            resp = ctx.env.sqs.receive_message(
                QueueUrl=ctx.env.ingest_queue, MaxNumberOfMessages=10, VisibilityTimeout=30
            )
            for m in resp.get("Messages", []):
                if a in m["Body"]:
                    ctx.env.sqs.delete_message(
                        QueueUrl=ctx.env.ingest_queue, ReceiptHandle=m["ReceiptHandle"]
                    )
                    return True
                ctx.env.sqs.change_message_visibility(
                    QueueUrl=ctx.env.ingest_queue,
                    ReceiptHandle=m["ReceiptHandle"],
                    VisibilityTimeout=0,
                )
            return False

        wait(drop, "the stream record", timeout=180)
    finally:
        _set_mapping(ctx, mapping, enabled=True)
    ctx.check(ctx.latest(a) == {}, "ingestion never saw the article", "UC7")

    print("   waiting out the reconciler's 5-minute stream grace period ...", flush=True)
    time.sleep(330)
    counts = ctx.reconcile()
    ctx.check(counts.get("requeued_drift", 0) >= 1, f"reconciler re-enqueued it: {counts}", "UC7")
    wait(lambda: ctx.latest(a).get("status") == "INDEXED", "UC7 indexed", timeout=300)
    summary = wait(lambda: settled(ctx, pid), "UC7 process")
    runs = ctx.env.runs(a)
    ctx.check(any(r.get("trigger") == "RECONCILER" for r in runs), "run triggered by RECONCILER",
              "UC7")  # fmt: skip
    ctx.check(summary.get("status") == "COMPLETE", f"process {summary.get('status')}", "UC7")
    ctx.state["uc7"] = a


def uc8(ctx: Ctx) -> None:
    print("UC8: article deleted in the producer -> rows gone, LATEST DELETED, 2 records")
    a = ctx.state.get("uc7") or ctx.state["dup"].article_id
    status, _ = ctx.producer_api.call("DELETE", f"/v1/articles/{a}")
    ctx.check(status == 204, f"producer delete ({status})", "UC8")
    latest = wait(lambda: in_status(ctx, a, "DELETED"), "UC8 delete", timeout=180)
    items = ctx.steps(a, str(latest["run_id"]))
    ctx.check(len(items) + 1 == 2, f"RUN# + {len(items)} step item = 2 records", "UC8")
    deleted = int(((items[0] if items else {}).get("metrics") or {}).get("rows_deleted", 0))
    ctx.check(deleted > 0, f"vector rows deleted: {deleted}", "UC8")


def uc6(ctx: Ctx) -> None:
    print("UC6: run interrupted mid-pipeline -> reconciler resumes it after the lease expires")
    result = ctx.ingest_files({f"uc6-{uuid.uuid4().hex[:8]}.txt": article_text("UC6", 60)},
                              "acceptance UC6")  # fmt: skip
    pid, a = result["process_id"], result["uploaded"][0]["article_id"]
    sfn = ctx.env.sfn

    def embedding() -> str | None:
        for e in ctx.env.executions(a, time.time() - 600):
            if e["status"] != "RUNNING":
                continue
            events = sfn.get_execution_history(executionArn=e["executionArn"], reverseOrder=True,
                                               maxResults=20)["events"]  # fmt: skip
            if any(ev.get("stateEnteredEventDetails", {}).get("name") == "Embed" for ev in events):
                return str(e["executionArn"])
        return None

    arn = wait(embedding, "the run to reach Embed", timeout=180, every=0.5)
    sfn.stop_execution(executionArn=arn, error="Acceptance.UC6", cause="interrupted on purpose")
    stopped = ctx.latest(a)
    ctx.check(stopped.get("status") not in ("INDEXED", None), f"interrupted at {stopped.get('stage')}",
              "UC6")  # fmt: skip

    expires = int(wait(lambda: ctx.latest(a).get("lease_expires_at"), "lease", timeout=120))
    print(f"   waiting {max(0, expires - int(time.time())) + 10}s for the lease to expire ...",
          flush=True)  # fmt: skip
    while True:  # Embed may still finish and extend the lease once
        expires = int(ctx.latest(a).get("lease_expires_at") or 0)
        if not expires or time.time() > expires + 5:
            break
        time.sleep(min(60, expires + 6 - time.time()))
    counts = ctx.reconcile()
    print(f"   reconciler: {counts}")
    wait(lambda: ctx.latest(a).get("status") == "INDEXED", "UC6 resumed run", timeout=600)
    latest = ctx.latest(a)
    resumed = sorted(str(i["sk"]) for i in ctx.steps(a, str(latest["run_id"])))
    runs = {str(r["run_id"]): r for r in ctx.env.runs(a)}
    ctx.check(runs[str(latest["run_id"])].get("trigger") == "RECONCILER",
              "resumed by the reconciler", "UC6")  # fmt: skip
    ctx.check(bool(resumed) and not resumed[0].endswith("FETCH"),
              f"resumed after the intact staged output: starts at {resumed[:1]}", "UC6")  # fmt: skip
    summary = wait(lambda: settled(ctx, pid), "UC6 process")
    ctx.check(ctx.counts(pid) == {"indexed": 1, "skipped": 0, "failed": 0},
              f"counted once: {ctx.counts(pid)}", "UC6")  # fmt: skip
    ctx.check(summary.get("status") == "COMPLETE", f"process {summary.get('status')}", "UC6")


SCENARIOS: dict[str, Callable[[Ctx], None]] = {
    "dup": dup_conc_uc1_uc11,
    "api": api,
    "uc3": uc3,
    "uc5": uc5,
    "uc7": uc7,
    "uc8": uc8,
    "uc6": uc6,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev")
    parser.add_argument("--only", default="", help="comma-separated scenarios")
    parser.add_argument("--skip", default="", help="comma-separated scenarios")
    args = parser.parse_args()
    only = {s for s in args.only.split(",") if s}
    skip = {s for s in args.skip.split(",") if s}
    ctx = Ctx(args.env)
    for name, scenario in SCENARIOS.items():
        if (only and name not in only and name != "dup") or name in skip:
            continue
        started = time.monotonic()
        try:
            scenario(ctx)
        except Exception as exc:
            ctx.failures.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"   FAIL {name}: {type(exc).__name__}: {exc}")
        print(f"   ({name}: {time.monotonic() - started:.0f}s)", flush=True)

    if ctx.failures:
        print("FAILED\n  " + "\n  ".join(ctx.failures))
        return 1
    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
