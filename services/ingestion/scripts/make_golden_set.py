"""Write a new synthetic golden set for retrieval evaluation (review M3.4) to staging.

    uv run python scripts/make_golden_set.py [--env dev] [--per-article 3] [--max-articles 50]
                                             [--no-answer 10] [--seed 7]

For each sampled indexed article, chunks are picked across the whole document (evenly spaced,
skipping tiny ones), and Amazon Nova Lite (Bedrock) writes one question that each chunk answers.
The question is labelled with that chunk_id (and the article's version, since chunk ids change
with it). A few questions the corpus can't answer are added as no_answer.

Each run writes _eval/golden/<YYYYMMDD-HHMM>.jsonl and never overwrites one; _eval/golden/LATEST
then names it, which is the set the eval function uses. Reports name the set they used, so
scores from different sets are never compared (the switch gate refuses that). Replace the
synthetic questions with real ones when you have them: same format, a new file, LATEST.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError
from gate_phase1 import tf_output

MODEL = "amazon.nova-lite-v1:0"
GOLDEN_DIR = "_eval/golden/"
POINTER = f"{GOLDEN_DIR}LATEST"
MIN_CHUNK_TOKENS = 40  # too little text for a specific question
PROMPT = (
    "Here is a passage from the article titled {title!r}.\n\n<passage>\n{text}\n</passage>\n\n"
    "Write one question that someone might search for and that this passage answers. Make it "
    "specific to the passage's details, phrased in your own words rather than copied. Reply with "
    "only the question."
)
# Questions this corpus is unlikely to answer: how high the best score goes without an answer.
NO_ANSWER = [
    "What is the boiling point of tungsten at sea level?",
    "How many moons does Neptune have?",
    "Who won the 1954 Tour de France?",
    "What is the recommended torque for a bicycle stem bolt?",
    "How do you propagate a fiddle-leaf fig from a cutting?",
    "What year was the first transatlantic telegraph cable laid?",
    "Which enzyme breaks down lactose in the small intestine?",
    "What is the capital of Burkina Faso?",
    "How long should sourdough bulk fermentation take at 24 degrees?",
    "What does a sommelier mean by a wine's legs?",
    "How many keys does a standard concert grand piano have?",
    "What is the half-life of carbon-14?",
]


def indexed_articles(ddb: Any, table: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "TableName": table,
        "IndexName": "gsi1-status",
        "KeyConditionExpression": "gsi1pk = :s",
        "ExpressionAttributeValues": {":s": {"S": "INDEXED"}},
    }
    while True:
        resp = ddb.query(**kwargs)
        items += resp["Items"]
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def staged_chunks(s3: Any, bucket: str, prefix: str) -> list[dict[str, Any]]:
    body = s3.get_object(Bucket=bucket, Key=f"{prefix}chunks/chunks.jsonl")["Body"].read()
    return [json.loads(line) for line in body.decode().splitlines() if line.strip()]


def spread(chunks: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """Up to n chunks evenly spaced over the document (beginning, middle and end)."""
    usable = [c for c in chunks if int(c.get("token_count", 0)) >= MIN_CHUNK_TOKENS] or chunks
    if len(usable) <= n:
        return usable
    step = (len(usable) - 1) / max(n - 1, 1)
    return [usable[round(i * step)] for i in range(n)]


def question(bedrock: Any, title: str, text: str) -> str | None:
    resp = bedrock.converse(
        modelId=MODEL,
        messages=[
            {"role": "user", "content": [{"text": PROMPT.format(title=title, text=text[:4000])}]}
        ],
        inferenceConfig={"maxTokens": 120, "temperature": 0.3},
    )
    raw = resp["output"]["message"]["content"][0]["text"].strip().strip('"')
    return raw if raw.endswith("?") and len(raw) > 10 else None


def exists(s3: Any, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev")
    parser.add_argument("--per-article", type=int, default=3)
    parser.add_argument("--max-articles", type=int, default=50)
    parser.add_argument("--no-answer", type=int, default=10)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    status_table = tf_output("ingestion", args.env, "status_table_name")
    bucket = tf_output("ingestion", args.env, "staging_bucket")
    ddb, s3, bedrock = boto3.client("dynamodb"), boto3.client("s3"), boto3.client("bedrock-runtime")
    key = f"{GOLDEN_DIR}{datetime.now(UTC).strftime('%Y%m%d-%H%M')}.jsonl"
    if exists(s3, bucket, key):
        print(f"{key} exists already; golden sets are never overwritten (try again in a minute)")
        return 1

    rng = random.Random(args.seed)
    articles = indexed_articles(ddb, status_table)
    rng.shuffle(articles)
    lines = []
    for item in articles[: args.max_articles]:
        article_id, prefix = item["pk"]["S"], item["staging_prefix"]["S"]
        version = int(item["doc_version"]["N"])
        try:
            chunks = staged_chunks(s3, bucket, prefix)
        except s3.exceptions.NoSuchKey:
            print(f"   skip {article_id}: no chunks staged")
            continue
        for chunk in spread(chunks, args.per_article):
            title = str((chunk.get("metadata") or {}).get("title") or "")
            q = question(bedrock, title, chunk["text"])
            if q is None:
                continue
            lines.append(
                {
                    "id": f"{article_id}-v{version}-{chunk['ordinal']}",
                    "question": q,
                    "article_id": article_id,
                    "doc_version": version,
                    "chunk_ids": [chunk["chunk_id"]],
                    "no_answer": False,
                }
            )
        print(f"   {article_id}: {len(lines)} question(s) so far", flush=True)
    for i, q in enumerate(rng.sample(NO_ANSWER, min(args.no_answer, len(NO_ANSWER)))):
        lines.append({"id": f"no-answer-{i}", "question": q, "no_answer": True})

    if not any(not line["no_answer"] for line in lines):
        print("no answerable questions written; nothing saved")
        return 1
    body = "\n".join(json.dumps(line) for line in lines) + "\n"
    s3.put_object(Bucket=bucket, Key=key, Body=body.encode(), ContentType="application/x-ndjson")
    s3.put_object(Bucket=bucket, Key=POINTER, Body=key.encode(), ContentType="text/plain")
    print(f"wrote {len(lines)} questions to s3://{bucket}/{key}; {POINTER} names it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
