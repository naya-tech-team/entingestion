"""Load test against a deployed environment: N new documents through the real producer, then
freshness (producer update -> indexed) from each article's LATEST, and throughput.

    uv run python scripts/load_test.py [--env dev] [--docs 10] [--words 400] [--timeout 1800]

Each document is unique text (a nonce), so every one is a new article that runs the whole
pipeline. It uploads with producer-cli (one process, closed), waits until every article is
terminal (INDEXED, SKIPPED or FAILED_*), and prints:

    freshness p50 / p95 / max   indexed_at - source_updated_at per article (the SLO: p95 < 15 min)
    throughput                  articles indexed per minute, first registration to last index
    outcome counts              and the ids of any that didn't index

With admission control, documents beyond live_slots wait on the ingest queue: watch
Dispatch_waiting_live and BacklogCount on the dashboard while it runs. Costs Bedrock calls
(about one per 400 words); keep --docs small on dev (account concurrency limit 10).
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

from acceptance import Env
from gate_phase1 import ROOT

SOURCE = "acceptance"
TERMINAL = {"INDEXED", "SKIPPED", "SKIPPED_UNCHANGED", "FAILED_PERMANENT", "FAILED_RETRYABLE"}
POLL_S = 10


def documents(directory: Path, count: int, words: int) -> None:
    for i in range(count):
        body = " ".join(
            f"Load test sentence {j} of document {i}, about ingestion throughput and freshness."
            for j in range(max(1, words // 12))
        )
        (directory / f"load-{i:04d}.txt").write_text(
            f"Load test document {i}\n\n{body}\n\nNonce {uuid.uuid4()}.\n"
        )


def upload(env: Env, directory: Path) -> tuple[str, list[str]]:
    run = subprocess.run(
        ["uv", "run", "producer-cli", "--api-url", env.api_url, "ingest-dir", str(directory),
         "--name", f"load test {len(list(directory.iterdir()))} docs", "--source", SOURCE,
         "--close"],
        cwd=ROOT / "services" / "producer", capture_output=True, text=True, check=True,
    )  # fmt: skip
    result = json.loads(run.stdout)
    return str(result["process_id"]), [str(u["article_id"]) for u in result["uploaded"]]


def epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", default="dev")
    parser.add_argument("--docs", type=int, default=10)
    parser.add_argument("--words", type=int, default=400)
    parser.add_argument("--timeout", type=int, default=1800, help="seconds to wait for indexing")
    args = parser.parse_args()
    env = Env(args.env)

    with tempfile.TemporaryDirectory() as tmp:
        documents(Path(tmp), args.docs, args.words)
        started = time.time()
        pid, article_ids = upload(env, Path(tmp))
    print(f"uploaded {len(article_ids)} documents in {time.time() - started:.0f} s (process {pid})")

    latest: dict[str, dict[str, object]] = {}
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        for article_id in article_ids:
            if str(latest.get(article_id, {}).get("status")) not in TERMINAL:
                latest[article_id] = env.item(article_id, "LATEST")
        done = sum(str(i.get("status")) in TERMINAL for i in latest.values())
        print(f"  {done}/{len(article_ids)} terminal after {time.time() - started:.0f} s")
        if done == len(article_ids):
            break
        time.sleep(POLL_S)

    lags, first, last = [], float("inf"), 0.0
    outcomes: dict[str, list[str]] = {}
    for article_id in article_ids:
        item = latest.get(article_id, {})
        outcomes.setdefault(str(item.get("status", "MISSING")), []).append(article_id)
        if item.get("status") == "INDEXED" and item.get("source_updated_at"):
            registered, indexed = (
                epoch(str(item["source_updated_at"])),
                epoch(str(item["indexed_at"])),
            )
            lags.append(indexed - registered)
            first, last = min(first, registered), max(last, indexed)

    print("\nOutcomes: " + ", ".join(f"{k} {len(v)}" for k, v in sorted(outcomes.items())))
    for status, ids in sorted(outcomes.items()):
        if status != "INDEXED":
            print(f"  {status}: {' '.join(ids[:20])}")
    if not lags:
        print("Nothing indexed: no freshness or throughput to report.")
        return 1
    print(
        f"Freshness (s): p50 {statistics.median(lags):.0f}, p95 {percentile(lags, 95):.0f},"
        f" max {max(lags):.0f}  (SLO: p95 < 900)"
    )
    span = max(last - first, 1.0)
    print(f"Throughput: {len(lags)} indexed in {span:.0f} s = {60 * len(lags) / span:.1f}/min")
    return 0 if len(lags) == len(article_ids) else 1


if __name__ == "__main__":
    sys.exit(main())
