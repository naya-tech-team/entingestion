"""One-off, after the review's producer changes (M2) reach an environment with existing data.

    uv run python scripts/backfill_review_m2.py --env dev [--apply]

1. Sets each article's tenant_id from its source's access policy (published by Terraform at
   /entingestion/<env>/producer/source_access), where it isn't set yet. Articles whose source
   has no policy are listed and left alone.
2. Gives existing process membership markers the gsi_member attributes, so listing a process's
   articles (which now reads them) still finds articles registered before the change.

Without --apply it only reports. Safe to re-run.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Any

import boto3
from botocore.exceptions import ClientError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    ssm = boto3.client("ssm")
    prefix = f"/entingestion/{args.env}/producer"
    policy = json.loads(ssm.get_parameter(Name=f"{prefix}/source_access")["Parameter"]["Value"])
    tenants = {name: cfg["tenant_id"] for name, cfg in policy.get("sources", {}).items()}
    table_name = ssm.get_parameter(Name=f"{prefix}/articles_table_name")["Parameter"]["Value"]
    table = boto3.resource("dynamodb").Table(table_name)

    counts: Counter[str] = Counter()
    kwargs: dict[str, object] = {"ProjectionExpression": "article_id, #s, tenant_id",
                                 "ExpressionAttributeNames": {"#s": "source"}}  # fmt: skip
    while True:
        page = table.scan(**kwargs)
        for item in page["Items"]:
            source = str(item.get("source", ""))
            if "tenant_id" in item:
                counts["already set"] += 1
            elif source not in tenants:
                counts[f"no policy for source {source!r}"] += 1
            elif args.apply:
                try:
                    table.update_item(
                        Key={"article_id": item["article_id"]},
                        UpdateExpression="SET tenant_id = :t",
                        ConditionExpression="attribute_exists(article_id)"
                        " AND attribute_not_exists(tenant_id)",
                        ExpressionAttributeValues={":t": tenants[source]},
                    )
                    counts[f"set {tenants[source]}"] += 1
                except ClientError as err:
                    if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
                        raise
                    counts["changed meanwhile"] += 1
            else:
                counts[f"would set {tenants[source]}"] += 1
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    uploads_name = table_name.replace("articles", "producer_uploads")
    memberships(boto3.resource("dynamodb").Table(uploads_name), counts, apply=args.apply)
    for key, n in sorted(counts.items()):
        print(f"  {key}: {n}")
    return 0


def memberships(uploads: Any, counts: Counter[str], *, apply: bool) -> None:
    kwargs: dict[str, Any] = {
        "FilterExpression": "kind = :m AND attribute_not_exists(member_process_id)",
        "ExpressionAttributeValues": {":m": "membership"},
    }
    while True:
        page = uploads.scan(**kwargs)
        for item in page["Items"]:
            if apply:
                uploads.update_item(
                    Key={"upload_id": item["upload_id"]},
                    UpdateExpression="SET member_process_id = :p, member_article_id = :a",
                    ExpressionAttributeValues={":p": item["process_id"], ":a": item["article_id"]},
                )
            counts["membership indexed" if apply else "membership would be indexed"] += 1
        if "LastEvaluatedKey" not in page:
            return
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


if __name__ == "__main__":
    sys.exit(main())
