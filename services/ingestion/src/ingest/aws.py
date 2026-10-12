"""Lazily created AWS clients, shared across invocations of a warm Lambda."""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config

from ingest.config import get_settings
from ingest.status.store import StatusStore

if TYPE_CHECKING:
    from mypy_boto3_bedrock_runtime.client import BedrockRuntimeClient
    from mypy_boto3_dynamodb.service_resource import DynamoDBServiceResource, Table
    from mypy_boto3_lambda.client import LambdaClient
    from mypy_boto3_s3.client import S3Client
    from mypy_boto3_secretsmanager.client import SecretsManagerClient
    from mypy_boto3_sqs.client import SQSClient
    from mypy_boto3_stepfunctions.client import SFNClient
    from mypy_boto3_textract.client import TextractClient

# Timeouts on every external call (brief §17); SDK retries are short because Step Functions
# retries at the step level with backoff.
_DEFAULT = Config(
    connect_timeout=5, read_timeout=30, retries={"mode": "standard", "max_attempts": 3}
)
_BEDROCK = Config(
    connect_timeout=5, read_timeout=60, retries={"mode": "adaptive", "max_attempts": 4}
)
_S3 = Config(
    connect_timeout=5,
    read_timeout=60,
    retries={"mode": "standard", "max_attempts": 3},
    s3={"addressing_style": "virtual"},
)


@lru_cache(maxsize=1)
def dynamodb() -> DynamoDBServiceResource:
    return boto3.resource("dynamodb", config=_DEFAULT)


def table(name: str) -> Table:
    return dynamodb().Table(name)


@lru_cache(maxsize=1)
def s3() -> S3Client:
    return boto3.client("s3", config=_S3)


@lru_cache(maxsize=1)
def bedrock() -> BedrockRuntimeClient:
    return boto3.client("bedrock-runtime", config=_BEDROCK)


@lru_cache(maxsize=1)
def sfn() -> SFNClient:
    return boto3.client("stepfunctions", config=_DEFAULT)


@lru_cache(maxsize=1)
def sqs() -> SQSClient:
    return boto3.client("sqs", config=_DEFAULT)


@lru_cache(maxsize=1)
def lambda_client() -> LambdaClient:
    return boto3.client("lambda", config=_DEFAULT)


@lru_cache(maxsize=1)
def secretsmanager() -> SecretsManagerClient:
    return boto3.client("secretsmanager", config=_DEFAULT)


@lru_cache(maxsize=1)
def textract() -> TextractClient:
    return boto3.client("textract", config=_DEFAULT)


# Clients without bundled type stubs (the dev group doesn't install them): untyped.


@lru_cache(maxsize=1)
def ssm() -> Any:
    return boto3.client("ssm", config=_DEFAULT)


@lru_cache(maxsize=1)
def bedrock_agent() -> Any:
    """bedrock-agent-runtime: the Rerank API."""
    return boto3.client("bedrock-agent-runtime", config=_BEDROCK)


@lru_cache(maxsize=1)
def comprehend() -> Any:
    return boto3.client("comprehend", config=_DEFAULT)


def status_store() -> StatusStore:
    settings = get_settings()
    return StatusStore(table(settings.status_table), settings)


def reset() -> None:
    """For tests: drop cached clients (e.g. after moto starts)."""
    for fn in (
        dynamodb,
        s3,
        bedrock,
        sfn,
        sqs,
        textract,
        secretsmanager,
        lambda_client,
        ssm,
        bedrock_agent,
        comprehend,
        get_settings,
    ):
        clear = getattr(fn, "cache_clear", None)  # a test may have swapped in a plain function
        if clear is not None:
            clear()
