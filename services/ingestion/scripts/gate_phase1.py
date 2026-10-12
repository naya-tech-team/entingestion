"""Phase 1 gate (brief §15): sample articles from the dev producer are indexed, and record counts
per run match §6.3.

    uv run python scripts/gate_phase1.py --corpus <dir> [--env dev] [--timeout-minutes 90]

1. Invokes the migrate Lambda (idempotent).
2. Ingests the corpus through the real producer (producer-cli ingest-dir --close).
3. Waits for PROCESS#<id>/SUMMARY to reach COMPLETE / COMPLETE_WITH_ERRORS.
4. Checks: every article reached an outcome, counts add up to the producer's registered_count,
   first runs wrote 10 records (RUN# + 8 steps + LATEST), DLQs are empty.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[3]


def tf_output(stack: str, env: str, name: str) -> str:
    return subprocess.check_output(
        ["terraform", "output", "-raw", name],
        cwd=ROOT / "terraform" / "stacks" / stack,
        env={**__import__("os").environ, "TF_DATA_DIR": f".terraform/{env}"},
        text=True,
    ).strip()


def main() -> int:  # noqa: PLR0912, PLR0915 - a linear checklist
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--env", default="dev")
    parser.add_argument("--timeout-minutes", type=int, default=90)
    args = parser.parse_args()

    lam = boto3.client("lambda")
    status = boto3.resource("dynamodb").Table(tf_output("ingestion", args.env, "status_table_name"))
    sqs = boto3.client("sqs")

    print("1. migrate:", end=" ", flush=True)
    resp = lam.invoke(FunctionName=tf_output("ingestion", args.env, "migrate_function_name"))
    payload = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        print("FAILED", payload)
        return 1
    print(payload)

    print("2. ingest through the producer ...", flush=True)
    api_url = tf_output("producer", args.env, "api_url")
    run = subprocess.run(
        [
            "uv",
            "run",
            "producer-cli",
            "--api-url",
            api_url,
            "ingest-dir",
            str(args.corpus.resolve()),
            "--name",
            "ingestion phase 1 gate",
            "--source",
            "gate1k",
            "--close",
        ],
        cwd=ROOT / "services" / "producer",
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        print(run.stdout[-2000:], run.stderr[-2000:])
        return 1
    result = json.loads(run.stdout)
    pid, uploaded = result["process_id"], len(result["uploaded"])
    print(f"   process {pid}: {uploaded} uploaded, {len(result['failed'])} failed")

    print("3. waiting for ingestion ...", flush=True)
    deadline = time.monotonic() + args.timeout_minutes * 60
    summary: dict[str, object] = {}
    while time.monotonic() < deadline:
        summary = (
            status.get_item(Key={"pk": f"PROCESS#{pid}", "sk": "SUMMARY"}, ConsistentRead=True).get(
                "Item"
            )
            or {}
        )
        counts = {k: int(summary.get(k, 0)) for k in ("indexed", "skipped", "failed")}  # type: ignore[call-overload]
        print(
            f"   {time.strftime('%H:%M:%S')} {counts} status={summary.get('status', '-')}",
            flush=True,
        )
        if summary.get("status") in ("COMPLETE", "COMPLETE_WITH_ERRORS"):
            break
        time.sleep(60)
    else:
        print("   timed out")

    print("4. checks")
    failures = []
    counts = Counter({k: int(summary.get(k, 0)) for k in ("indexed", "skipped", "failed")})  # type: ignore[call-overload]
    if summary.get("status") not in ("COMPLETE", "COMPLETE_WITH_ERRORS"):
        failures.append(f"process not complete: {dict(counts)}")
    if sum(counts.values()) != uploaded:
        failures.append(f"outcomes {sum(counts.values())} != uploaded {uploaded}")

    members = status.query(
        KeyConditionExpression="pk = :p AND begins_with(sk, :a)",
        ExpressionAttributeValues={":p": f"PROCESS#{pid}", ":a": "ARTICLE#"},
    )["Items"]
    sample = [m["sk"].removeprefix("ARTICLE#") for m in members[:25]]
    record_counts = Counter()
    for aid in sample:
        items = status.query(
            KeyConditionExpression="pk = :p",
            ExpressionAttributeValues={":p": aid},
            ConsistentRead=True,
        )["Items"]
        latest = next(i for i in items if i["sk"] == "LATEST")
        runs = [i for i in items if i["sk"].startswith("RUN#") and "#STEP#" not in i["sk"]]
        for r in runs:
            n = sum(1 for i in items if i["sk"].startswith(r["sk"]))
            record_counts[(r.get("outcome"), n)] += 1
        if latest["status"] != "INDEXED" or int(latest.get("chunk_count", 0)) < 1:
            failures.append(f"{aid}: LATEST {latest['status']} chunks={latest.get('chunk_count')}")
    print(
        f"   runs in a {len(sample)}-article sample, by (outcome, RUN#+step items): {dict(record_counts)}"
    )
    if any(outcome == "INDEXED" and n != 9 for outcome, n in record_counts):
        failures.append("an INDEXED first run didn't write RUN# + 8 step items (+ LATEST = 10)")

    for name in ("ingest_dlq_url", "pipe_dlq_url"):
        url = tf_output("ingestion", args.env, name)
        depth = int(
            sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])[
                "Attributes"
            ]["ApproximateNumberOfMessages"]
        )
        print(f"   {name}: {depth} message(s)")
        if depth:
            failures.append(f"{name} has {depth} message(s)")

    if failures:
        print("GATE FAILED\n  " + "\n  ".join(failures))
        return 1
    print(f"GATE PASSED: {dict(counts)} for process {pid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
