"""Acceptance checks against a deployed environment: DUP and CONC (docs/USE_CASES.md).

    uv run python scripts/acceptance.py [dup|conc|all] [--env dev]

DUP:  one new article through the real producer; its stream record is then sent to the ingest
      queue twice more (3 deliveries). Expect one execution and one run.
CONC: two forced runs for that article started at the same instant. Expect one to win the lease
      and index; the other ends quietly (RUN# outcome STALE, execution SUCCEEDED, nothing failed),
      and the process counters don't move.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import boto3
from _tf import ROOT, default_env, tf_output
from boto3.dynamodb.types import TypeDeserializer

from ingest.ids import new_run_id

# The producer source the acceptance scenarios upload to (the producer must know it).
SOURCE = os.environ.get("ACCEPTANCE_SOURCE", "acceptance")
_DESERIALIZER = TypeDeserializer()
POLL_S = 5


class Env:
    def __init__(self, env: str) -> None:
        out = lambda name: tf_output("ingestion", env, name)  # noqa: E731
        self.env = env
        self.ddb = boto3.client("dynamodb")
        self.sfn = boto3.client("stepfunctions")
        self.sqs = boto3.client("sqs")
        self.status_table = out("status_table_name")
        self.state_machine = out("state_machine_arn")
        self.ingest_queue = out("ingest_queue_url")
        self.api_url = tf_output("producer", env, "api_url")
        self.articles_table = tf_output("producer", env, "articles_table_name")

    def item(self, pk: str, sk: str) -> dict[str, Any]:
        resp = self.ddb.get_item(
            TableName=self.status_table,
            Key={"pk": {"S": pk}, "sk": {"S": sk}},
            ConsistentRead=True,
        )
        return plain(resp.get("Item", {}))

    def runs(self, article_id: str) -> list[dict[str, Any]]:
        resp = self.ddb.query(
            TableName=self.status_table,
            KeyConditionExpression="pk = :p AND begins_with(sk, :r)",
            ExpressionAttributeValues={":p": {"S": article_id}, ":r": {"S": "RUN#"}},
            ConsistentRead=True,
        )
        return [plain(i) for i in resp["Items"] if "#STEP#" not in i["sk"]["S"]]

    def executions(self, article_id: str, since: float) -> list[dict[str, Any]]:
        found = []
        for page in self.sfn.get_paginator("list_executions").paginate(
            stateMachineArn=self.state_machine
        ):
            for e in page["executions"]:
                if e["startDate"].timestamp() < since:
                    return found
                if e["name"].startswith(f"{article_id}-"):
                    found.append(e)
        return found

    def wait_executions(self, article_id: str, since: float, count: int) -> list[dict[str, Any]]:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            found = self.executions(article_id, since)
            if len(found) >= count and all(e["status"] != "RUNNING" for e in found):
                time.sleep(POLL_S)  # let late duplicates (if any) show up too
                return self.executions(article_id, since)
            time.sleep(POLL_S)
        raise TimeoutError(f"executions for {article_id} didn't finish")


def plain(item: dict[str, Any]) -> dict[str, Any]:
    return {k: _DESERIALIZER.deserialize(v) for k, v in item.items()}


def check(failures: list[str], ok: bool, label: str) -> None:
    print(f"   {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        failures.append(label)


class Dup(NamedTuple):
    failures: list[str]
    article_id: str
    process_id: str
    filename: str
    text: str


def dup(env: Env) -> Dup:
    print("DUP: same stream record delivered 3x")
    failures: list[str] = []
    filename = f"dup-{uuid.uuid4().hex[:8]}.txt"
    text = (
        "Acceptance check DUP.\n\n"
        + " ".join(f"Sentence {i} of a short article about duplicate delivery." for i in range(60))
        + f"\n\nNonce {uuid.uuid4()}.\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / filename).write_text(text)
        run = subprocess.run(
            ["uv", "run", "producer-cli", "--api-url", env.api_url, "ingest-dir", tmp,
             "--name", "acceptance DUP", "--source", SOURCE, "--close"],
            cwd=ROOT / "services" / "producer", capture_output=True, text=True, check=True,
        )  # fmt: skip
    result = json.loads(run.stdout)
    pid, uploaded = result["process_id"], result["uploaded"][0]
    article_id, upload_id = uploaded["article_id"], uploaded["upload_id"]
    since = time.time() - 5
    print(f"   process {pid}, article {article_id}")

    # The registered row, as the Pipe forwards it (DynamoDB JSON NewImage).
    deadline = time.monotonic() + 120
    row: dict[str, Any] = {}
    while time.monotonic() < deadline:
        row = env.ddb.get_item(
            TableName=env.articles_table, Key={"article_id": {"S": article_id}}, ConsistentRead=True
        ).get("Item", {})
        if row.get("upload_id", {}).get("S") == upload_id:
            break
        time.sleep(1)
    else:
        raise TimeoutError("article row never registered")
    record = {
        "eventName": "INSERT",
        "dynamodb": {"Keys": {"article_id": row["article_id"]}, "NewImage": row},
    }
    for _ in range(2):  # + the Pipe's own delivery = 3
        env.sqs.send_message(QueueUrl=env.ingest_queue, MessageBody=json.dumps(record))

    executions = env.wait_executions(article_id, since, 1)
    runs = [r for r in env.runs(article_id) if r.get("process_id") == pid]
    summary = env.item(f"PROCESS#{pid}", "SUMMARY")
    check(failures, len(executions) == 1, f"one execution (got {len(executions)})")
    check(failures, all(e["status"] == "SUCCEEDED" for e in executions), "it succeeded")
    check(failures, len(runs) == 1, f"one run (got {len(runs)})")
    check(failures, runs[0].get("outcome") == "INDEXED" if runs else False, "run outcome INDEXED")
    check(
        failures,
        int(summary.get("indexed", 0)) == 1,
        f"process counted once: {summary.get('indexed')}",
    )
    check(
        failures, summary.get("status") == "COMPLETE", f"process COMPLETE: {summary.get('status')}"
    )
    return Dup(failures, article_id, pid, filename, text)


def conc(env: Env, article_id: str, pid: str) -> list[str]:
    print("CONC: two runs for one article at the same instant")
    failures: list[str] = []
    before = env.item(article_id, "LATEST")
    summary_before = env.item(f"PROCESS#{pid}", "SUMMARY")
    row = plain(
        env.ddb.get_item(TableName=env.articles_table, Key={"article_id": {"S": article_id}})[
            "Item"
        ]
    )
    message = {
        "article_id": article_id,
        "process_id": pid,
        "event": "UPSERT",
        "s3_uri": row["s3_uri"],
        "s3_version_id": row.get("s3_version_id"),
        "content_sha256": row["content_sha256"],
        "source_updated_at": row["updated_at"],
        "trigger": "MANUAL",
        "requested_by": "acceptance:conc",
        "force": True,
    }
    since = time.time() - 5
    tag = uuid.uuid4().hex[:8]

    def start(i: int) -> str:
        run_id = new_run_id()
        payload = {"ctx": {"message": message, "run_id": run_id}}
        env.sfn.start_execution(
            stateMachineArn=env.state_machine,
            name=f"{article_id}-conc-{tag}-{i}",
            input=json.dumps(payload),
        )
        return run_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        run_ids = list(pool.map(start, range(2)))

    executions = env.wait_executions(article_id, since, 2)
    runs = {r["run_id"]: r for r in env.runs(article_id) if r["run_id"] in run_ids}
    outcomes = sorted(str(r.get("outcome")) for r in runs.values())
    latest = env.item(article_id, "LATEST")
    summary = env.item(f"PROCESS#{pid}", "SUMMARY")
    check(failures, len(executions) == 2, f"two executions (got {len(executions)})")
    check(failures, all(e["status"] == "SUCCEEDED" for e in executions),
          f"none failed: {[e['status'] for e in executions]}")  # fmt: skip
    check(failures, outcomes == ["INDEXED", "STALE"], f"one INDEXED, one STALE: {outcomes}")
    check(failures, latest.get("status") == "INDEXED", f"LATEST INDEXED: {latest.get('status')}")
    check(failures, int(latest["doc_version"]) == int(before["doc_version"]) + 1,
          f"doc_version {before['doc_version']} -> {latest['doc_version']}")  # fmt: skip
    check(failures, "lease_owner" not in latest, "lease released")
    counters = ("indexed", "skipped", "failed")
    check(failures, {k: summary.get(k) for k in counters} == {k: summary_before.get(k) for k in counters},
          "process counters unchanged")  # fmt: skip
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("which", nargs="?", default="all", choices=["dup", "conc", "all"])
    parser.add_argument("--env", default=default_env())
    parser.add_argument("--article-id", help="conc only: an INDEXED article")
    parser.add_argument("--process-id", help="conc only: its process")
    args = parser.parse_args()
    env = Env(args.env)
    print(f"{datetime.now(UTC):%H:%M:%S} env={args.env}")

    failures: list[str] = []
    article_id, pid = args.article_id, args.process_id
    if args.which in ("dup", "all"):
        result = dup(env)
        article_id, pid = result.article_id, result.process_id
        failures += [f"DUP: {f}" for f in result.failures]
    if args.which in ("conc", "all"):
        if not (article_id and pid):
            parser.error("conc needs --article-id and --process-id (or run all)")
        failures += [f"CONC: {f}" for f in conc(env, article_id, pid)]

    if failures:
        print("FAILED\n  " + "\n  ".join(failures))
        return 1
    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
