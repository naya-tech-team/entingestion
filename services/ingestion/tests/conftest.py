"""moto-backed AWS for unit tests (tables mirror terraform/stacks/ingestion), and the embedding
profiles every test runs with."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.client import BaseClient
from moto import mock_aws


def _test_region() -> str:
    """The region the tests run in, from the environment: AWS_REGION, else AWS_DEFAULT_REGION,
    else the AWS config's region. moto works in any region; nothing here names one."""
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    region = region or boto3.session.Session().region_name
    if not region:
        raise RuntimeError("set AWS_REGION (or AWS_DEFAULT_REGION) to run the tests")
    return region


REGION = _test_region()


def bucket_location() -> dict[str, Any]:
    """create_bucket's location argument: S3 refuses a LocationConstraint of us-east-1
    (its default region)."""
    return (
        {}
        if REGION == "us-east-1"
        else {"CreateBucketConfiguration": {"LocationConstraint": REGION}}
    )


STAGING_BUCKET = "ingest-staging-test"
RAW_BUCKET = "producer-raw-test"

# The embedding profiles of every test (EMBEDDING_PROFILES has no default): Titan v2 at 1024 and
# 512 dimensions, primary being the default registry's table.
TITAN = "amazon.titan-embed-text-v2:0"
PROFILES = {
    "primary": {"provider": "bedrock", "model": TITAN, "dimension": 1024},
    "compact": {"provider": "bedrock", "model": TITAN, "dimension": 512},
}
PROFILE_ENV = {
    "EMBEDDING_PROFILES": json.dumps(PROFILES),
    "DEFAULT_PROFILE": "primary",
    "TENANT_PROFILES": "{}",
}

ENV = {
    "AWS_DEFAULT_REGION": REGION,
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "PIPELINE_VERSION": "v1",
    "STATUS_TABLE": "ingestion_status",
    "CACHE_TABLE": "embedding_cache",
    "STAGING_BUCKET": STAGING_BUCKET,
    "PRODUCER_ARTICLES_TABLE": "articles",
    "PRODUCER_PROCESS_TABLE": "process",
    "STATE_MACHINE_ARN": f"arn:aws:states:{REGION}:123456789012:stateMachine:ingest",
    "LEASE_SECONDS": "900",
    "POWERTOOLS_SERVICE_NAME": "ingestion",
    "POWERTOOLS_METRICS_NAMESPACE": "Ingestion",
    "POWERTOOLS_TRACE_DISABLED": "1",
    "TABLES_FUNCTION_NAME": "ingestion-test-tables",
    "QUERY_FUNCTION_NAME": "ingestion-test-query",
    "DB_HOST": "db.test.invalid",
}

STATUS_GSIS = [
    ("gsi1-status", "gsi1pk", "gsi1sk", "S"),
    ("gsi2-process", "gsi2pk", "gsi2sk", "S"),
    ("gsi3-lease", "gsi3pk", "gsi3sk", "N"),
    ("gsi4-stepfail", "gsi4pk", "gsi4sk", "S"),
]


def create_tables() -> None:
    ddb = boto3.client("dynamodb", region_name=REGION)
    attrs = [
        {"AttributeName": "pk", "AttributeType": "S"},
        {"AttributeName": "sk", "AttributeType": "S"},
    ]
    for _, h, r, rtype in STATUS_GSIS:
        attrs += [
            {"AttributeName": h, "AttributeType": "S"},
            {"AttributeName": r, "AttributeType": rtype},
        ]
    ddb.create_table(
        TableName="ingestion_status",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=attrs,
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": name,
                "KeySchema": [
                    {"AttributeName": h, "KeyType": "HASH"},
                    {"AttributeName": r, "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
            for name, h, r, _ in STATUS_GSIS
        ],
    )
    ddb.create_table(
        TableName="embedding_cache",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "cache_key", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "cache_key", "KeyType": "HASH"}],
    )
    ddb.create_table(
        TableName="articles",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "article_id", "AttributeType": "S"},
            {"AttributeName": "updated_at", "AttributeType": "S"},
            {"AttributeName": "updated_shard", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "article_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "gsi_updated_shard",
                "KeySchema": [
                    {"AttributeName": "updated_shard", "KeyType": "HASH"},
                    {"AttributeName": "updated_at", "KeyType": "RANGE"},
                ],
                "Projection": {
                    "ProjectionType": "INCLUDE",
                    "NonKeyAttributes": ["content_sha256", "process_id", "upload_id"],
                },
            }
        ],
    )
    ddb.create_table(
        TableName="process",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "process_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "process_id", "KeyType": "HASH"}],
    )


def create_queues(monkeypatch: pytest.MonkeyPatch) -> None:
    sqs = boto3.client("sqs", region_name=REGION)
    for name, env in (
        ("ingest", "INGEST_QUEUE_URL"),
        ("ingest-dlq", "INGEST_DLQ_URL"),
        ("backfill", "BACKFILL_QUEUE_URL"),
    ):
        monkeypatch.setenv(env, sqs.create_queue(QueueName=name)["QueueUrl"])


_EXPRESSIONS = (
    "UpdateExpression",
    "ConditionExpression",
    "KeyConditionExpression",
    "FilterExpression",
    "ProjectionExpression",
)


def check_expression_names(operation: str, params: dict[str, Any]) -> None:
    """DynamoDB rejects ExpressionAttributeNames/Values an expression doesn't use; moto doesn't.
    Check every request the way DynamoDB does, so such bugs fail here, not in an environment."""
    requests = (
        [next(iter(item.values())) for item in params.get("TransactItems", [])]
        if operation == "TransactWriteItems"
        else [params]
    )
    for request in requests:
        text = " ".join(str(request.get(k, "")) for k in _EXPRESSIONS)
        for kind in ("ExpressionAttributeNames", "ExpressionAttributeValues"):
            unused = [
                k for k in request.get(kind) or {} if not re.search(re.escape(k) + r"\b", text)
            ]
            if unused:
                raise AssertionError(f"{operation}: {kind} unused in expressions: {unused}")


@pytest.fixture(autouse=True)
def embedding_profiles(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts from the two Titan profiles (a test may set others)."""
    from ingest.config import get_settings

    for key, value in PROFILE_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    from ingest import access
    from ingest import aws as ingest_aws

    real_call = BaseClient._make_api_call

    def checked_call(self: Any, operation: str, params: dict[str, Any]) -> Any:
        if self.meta.service_model.service_name == "dynamodb":
            check_expression_names(operation, params)
        return real_call(self, operation, params)

    monkeypatch.setattr(BaseClient, "_make_api_call", checked_call)

    access.reset()

    with mock_aws():
        create_tables()
        create_queues(monkeypatch)
        ingest_aws.reset()
        s3 = boto3.client("s3", region_name=REGION)
        for bucket in (STAGING_BUCKET, RAW_BUCKET):
            s3.create_bucket(Bucket=bucket, **bucket_location())
            s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        yield
        ingest_aws.reset()
