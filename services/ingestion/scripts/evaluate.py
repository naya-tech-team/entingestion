"""Run the retrieval evaluation (chunk recall@5/@10, nDCG@10, MRR with 95% CIs) on a deployed
environment.

    uv run python scripts/evaluate.py [--env dev] [--table chunks_compact] [--baseline]
                                      [--min-recall10 0.6]

Without --table it scores the live table (chunks_active); with one, a reindexed table before
switching to it (brief §7.3). --baseline also scores the live table on the same golden set and
prints the difference: the switch (POST /v1/tables/active) needs both reports. --min-recall10
makes it a gate (exit 1 below the threshold), e.g. in a CI job with AWS credentials.
"""

from __future__ import annotations

import argparse
import json
import sys

import boto3
from gate_phase1 import tf_output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev")
    parser.add_argument("--table", default=None)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--min-recall10", type=float, default=None)
    args = parser.parse_args()

    resp = boto3.client("lambda").invoke(
        FunctionName=tf_output("ingestion", args.env, "eval_function_name"),
        Payload=json.dumps({"table": args.table, "baseline": args.baseline}).encode(),
    )
    out = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        print(f"evaluation failed: {out}")
        return 1
    print(json.dumps(out, indent=2))
    if args.min_recall10 is not None and out["recall_at_10"] < args.min_recall10:
        print(f"recall@10 {out['recall_at_10']:.3f} < {args.min_recall10}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
