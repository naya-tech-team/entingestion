from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import boto3
import pytest
from botocore.client import BaseClient
from moto import mock_aws

REGION = "us-west-2"
BUCKET = "producer-test-raw"
CALLER_ARN = "arn:aws:iam::123456789012:user/tester"  # writes "news"
OTHER_ARN = "arn:aws:sts::123456789012:assumed-role/sports-writer/s1"  # writes "sports"
READER_ARN = "arn:aws:sts::123456789012:assumed-role/news-search/s1"  # reads "news"
ADMIN_ARN = "arn:aws:sts::123456789012:assumed-role/entingestion-test-deploy/ops"
SOURCE_ACCESS = {
    "sources": {
        "news": {
            "tenant_id": "t-news",
            "writers": [CALLER_ARN],
            "readers": ["arn:aws:iam::123456789012:role/news-*"],
        },
        "blog": {"tenant_id": "t-blog", "writers": [CALLER_ARN]},
        "smoke": {"tenant_id": "t-smoke", "writers": [CALLER_ARN]},
        "sports": {
            "tenant_id": "t-sports",
            "writers": ["arn:aws:iam::123456789012:role/sports-writer"],
        },
    },
    "admins": ["arn:aws:iam::123456789012:role/entingestion-*-deploy"],
}

ENV = {
    "AWS_DEFAULT_REGION": REGION,
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "PROCESS_TABLE": "process",
    "ARTICLES_TABLE": "articles",
    "UPLOADS_TABLE": "producer_uploads",
    "RAW_BUCKET": BUCKET,
    "RAW_PREFIX": "raw/",
    "MAX_FILE_BYTES": str(10 * 1024 * 1024),
    "POWERTOOLS_SERVICE_NAME": "producer",
    "POWERTOOLS_METRICS_NAMESPACE": "Producer",
    "POWERTOOLS_TRACE_DISABLED": "1",
    "SOURCE_ACCESS": json.dumps(SOURCE_ACCESS),
    "IDEMPOTENCY_TABLE": "producer_idempotency",
}


# gsi_process_uploads (terraform/stacks/producer/main.tf): what GET /v1/processes/{id}/uploads
# returns, plus `kind` to leave membership markers out.
PROCESS_UPLOADS_ATTRIBUTES = [
    "article_id", "filename", "content_type", "size_bytes", "status", "reason", "title",
    "external_id", "finished_at", "kind",
]  # fmt: skip


def _create_tables() -> None:
    """Mirror terraform/services/producer/main.tf."""
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(
        TableName="process",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": name, "AttributeType": "S"}
            for name in ("process_id", "source", "created_at")
        ],
        KeySchema=[{"AttributeName": "process_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "gsi_source",
                "KeySchema": [
                    {"AttributeName": "source", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    ddb.create_table(
        TableName="producer_idempotency",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
    )
    ddb.create_table(
        TableName="articles",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": name, "AttributeType": "S"}
            for name in ("article_id", "process_id", "updated_at", "updated_date", "updated_shard")
        ],
        KeySchema=[{"AttributeName": "article_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "gsi_process",
                "KeySchema": [
                    {"AttributeName": "process_id", "KeyType": "HASH"},
                    {"AttributeName": "updated_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "gsi_updated",
                "KeySchema": [
                    {"AttributeName": "updated_date", "KeyType": "HASH"},
                    {"AttributeName": "updated_at", "KeyType": "RANGE"},
                ],
                "Projection": {
                    "ProjectionType": "INCLUDE",
                    "NonKeyAttributes": ["content_sha256", "process_id", "upload_id"],
                },
            },
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
            },
        ],
        StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"},
    )
    ddb.create_table(
        TableName="producer_uploads",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "upload_id", "AttributeType": "S"},
            {"AttributeName": "member_process_id", "AttributeType": "S"},
            {"AttributeName": "member_article_id", "AttributeType": "S"},
            {"AttributeName": "pending", "AttributeType": "S"},
            {"AttributeName": "ticket_expires_epoch", "AttributeType": "N"},
            {"AttributeName": "process_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "upload_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "gsi_member",
                "KeySchema": [
                    {"AttributeName": "member_process_id", "KeyType": "HASH"},
                    {"AttributeName": "member_article_id", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            },
            {
                "IndexName": "gsi_pending",
                "KeySchema": [
                    {"AttributeName": "pending", "KeyType": "HASH"},
                    {"AttributeName": "ticket_expires_epoch", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "gsi_process_uploads",
                "KeySchema": [
                    {"AttributeName": "process_id", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {
                    "ProjectionType": "INCLUDE",
                    "NonKeyAttributes": PROCESS_UPLOADS_ATTRIBUTES,
                },
            },
        ],
    )


_EXPRESSIONS = (
    "UpdateExpression",
    "ConditionExpression",
    "KeyConditionExpression",
    "FilterExpression",
    "ProjectionExpression",
)


def _check_expression_names(operation: str, params: dict[str, Any]) -> None:
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


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    real_call = BaseClient._make_api_call

    def checked_call(self: Any, operation: str, params: dict[str, Any]) -> Any:
        if self.meta.service_model.service_name == "dynamodb":
            _check_expression_names(operation, params)
        return real_call(self, operation, params)

    monkeypatch.setattr(BaseClient, "_make_api_call", checked_call)
    with mock_aws():
        _create_tables()
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        # Mirrors the Terraform bucket (versioned), so articles get s3_version_id.
        s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        from producer import api, config, registrar

        caches = (
            config.get_settings,
            api.get_service,
            api._idempotency_store,
            registrar.get_registrar,
        )
        for cache in caches:
            cache.cache_clear()
        yield
        for cache in caches:
            cache.cache_clear()


@dataclass
class FakeContext:
    function_name: str = "producer-api"
    memory_limit_in_mb: int = 512
    invoked_function_arn: str = "arn:aws:lambda:us-west-2:123456789012:function:producer-api"
    aws_request_id: str = "00000000-0000-0000-0000-000000000000"

    def get_remaining_time_in_millis(self) -> int:
        return 29_000


@dataclass
class ApiResponse:
    status: int
    body: Any
    headers: dict[str, str]


ApiCall = Callable[..., ApiResponse]


def rest_event(
    method: str,
    path: str,
    body: Any = None,
    query: dict[str, str] | None = None,
    raw_body: str | None = None,
    *,
    caller: str = CALLER_ARN,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    if raw_body is None and body is not None:
        raw_body = json.dumps(body)
    all_headers = {"Content-Type": "application/json", **(headers or {})}
    return {
        "resource": "/{proxy+}",
        "path": path,
        "httpMethod": method,
        "headers": all_headers,
        "multiValueHeaders": {k: [v] for k, v in all_headers.items()},
        "queryStringParameters": query,
        "multiValueQueryStringParameters": {k: [v] for k, v in query.items()} if query else None,
        "pathParameters": {"proxy": path.lstrip("/")},
        "stageVariables": None,
        "requestContext": {
            "resourcePath": "/{proxy+}",
            "httpMethod": method,
            "path": f"/prod{path}",
            "stage": "prod",
            "requestId": "req-123",
            "accountId": "123456789012",
            "apiId": "abc123",
            "identity": {
                "userArn": caller,
                "caller": "AIDAEXAMPLE",
                "sourceIp": "203.0.113.10",
            },
        },
        "body": raw_body,
        "isBase64Encoded": False,
    }


@pytest.fixture
def api(aws: None) -> ApiCall:
    from producer.api import lambda_handler

    def call(
        method: str,
        path: str,
        body: Any = None,
        query: dict[str, str] | None = None,
        raw_body: str | None = None,
        *,
        caller: str = CALLER_ARN,
        headers: dict[str, str] | None = None,
    ) -> ApiResponse:
        event = rest_event(method, path, body, query, raw_body, caller=caller, headers=headers)
        resp = lambda_handler(event, FakeContext())  # type: ignore[arg-type]
        text = resp.get("body") or ""
        headers = resp.get("headers") or {
            k: v[-1] for k, v in (resp.get("multiValueHeaders") or {}).items()
        }
        return ApiResponse(resp["statusCode"], json.loads(text) if text else None, headers)

    return call
