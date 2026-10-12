"""One-off, before the vector-table registry goes live in an environment that already has data.

    uv run python scripts/backfill_table_versions.py --env dev [--apply]

1. Seeds #CONFIG/VECTOR_TABLES with the table chunks_active serves (writes = [live]), unless
   the registry already exists.
2. For every vector table, records LATEST.tv_<table> = doc_version for articles whose active
   rows in that table are the version LATEST holds. The switch gate and the drift check use
   these markers; without them every existing article would look missing.

Without --apply it only reports what it would write. Safe to re-run.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr
from gate_phase1 import tf_output

TABLES = ("chunks_titan2_1024", "chunks_titan2_512")


def invoke(function: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = boto3.client("lambda").invoke(
        FunctionName=function, Payload=json.dumps(payload).encode()
    )
    out = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        raise RuntimeError(f"{function} {payload['action']}: {out}")
    return dict(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    status = boto3.resource("dynamodb").Table(tf_output("ingestion", args.env, "status_table_name"))
    query = f"ingestion-{args.env}-query"
    live = invoke(query, {"action": "describe"})["active_table"]
    print(f"chunks_active serves {live}")

    registry = status.get_item(Key={"pk": "#CONFIG", "sk": "VECTOR_TABLES"}).get("Item")
    if registry:
        print(f"registry exists: live={registry['live']} writes={registry['writes']}")
    elif args.apply:
        status.put_item(
            Item={"pk": "#CONFIG", "sk": "VECTOR_TABLES", "live": live, "writes": [live],
                  "epoch": 1, "updated_by": "backfill_table_versions"},
            ConditionExpression="attribute_not_exists(pk)",
        )  # fmt: skip
        print(f"registry seeded: live={live} writes=[{live}]")
    else:
        print(f"would seed the registry: live={live} writes=[{live}]")

    counts: Counter[str] = Counter()
    for table in TABLES:
        after = ""
        while True:
            page = invoke(query, {"action": "versions", "table": table, "after": after})
            for article_id, version, _ in page["rows"]:
                latest = status.get_item(Key={"pk": article_id, "sk": "LATEST"}).get("Item")
                if not latest or int(str(latest.get("doc_version", -1))) != version:
                    counts[f"{table}: not the current version"] += 1
                    continue
                if args.apply:
                    status.update_item(
                        Key={"pk": article_id, "sk": "LATEST"},
                        UpdateExpression="SET #tv = :v",
                        ConditionExpression=Attr("doc_version").eq(version),
                        ExpressionAttributeNames={"#tv": f"tv_{table}"},
                        ExpressionAttributeValues={":v": version},
                    )
                counts[f"{table}: {'marked' if args.apply else 'would mark'}"] += 1
            after = page["next"] or ""
            if not after:
                break
    for key, n in sorted(counts.items()):
        print(f"  {key}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
