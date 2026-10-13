"""HTTP plumbing shared by the control API's routes: JSON responses, request bodies, query
parameters, the caller's identity, paging tokens and console links."""

from __future__ import annotations

import json
from typing import Any, cast

from aws_lambda_powertools.event_handler import Response, content_types
from aws_lambda_powertools.utilities.data_classes import APIGatewayProxyEvent
from aws_lambda_powertools.utilities.data_classes.common import BaseProxyEvent
from entcommon.paging import TokenError, decode_token
from pydantic import BaseModel, ConfigDict, ValidationError

from ingest import aws
from ingest.models.status import ArticleStatus
from ingest.vectorstore import registry

# GET /v1/articles?status=
DEFAULT_PAGE, MAX_PAGE = 50, 200
_PAGE_KEYS = frozenset({"pk", "sk", "gsi1pk", "gsi1sk"})  # gsi1-status LastEvaluatedKey
MAX_TENANT_ID = 128  # as IngestMessage.tenant_id


class Request(BaseModel):
    """A request body: unknown fields are refused."""

    model_config = ConfigDict(extra="forbid")


class BadRequest(Exception):
    """422 with the validation errors."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("request validation failed")
        self.errors = errors


def json_response(status: int, body: Any) -> Response[str]:
    return Response(
        status_code=status,
        content_type=content_types.APPLICATION_JSON,
        body=json.dumps(body, separators=(",", ":"), default=str),
    )


def body[ModelT: BaseModel](event: BaseProxyEvent, model: type[ModelT]) -> ModelT:
    raw = event.decoded_body or ""
    try:
        data = json.loads(raw) if raw.strip() else {}
        return model.model_validate(data)
    except json.JSONDecodeError as err:
        raise BadRequest([{"loc": ["body"], "msg": f"invalid JSON: {err.msg}"}]) from err
    except ValidationError as err:
        raise BadRequest(
            [{"loc": list(e["loc"]), "msg": e["msg"]} for e in err.errors(include_url=False)]
        ) from err


def caller(event: BaseProxyEvent) -> str:
    """The IAM identity API Gateway authorized (a REST API event)."""
    identity = cast(APIGatewayProxyEvent, event).request_context.identity
    return identity.user_arn or identity.caller or "unknown"


def tenant_param(event: BaseProxyEvent) -> str | None:
    """?tenant_id= as a registry key (None: the default registry)."""
    value = event.get_query_string_value("tenant_id") or None
    if value is not None and len(value) > MAX_TENANT_ID:
        raise BadRequest([{"loc": ["query", "tenant_id"], "msg": "at most 128 characters"}])
    return registry.scope(value)


def status_param(event: BaseProxyEvent) -> ArticleStatus:
    value = event.get_query_string_value("status") or ""
    if not value:
        raise BadRequest([{"loc": ["query", "status"], "msg": "status is required"}])
    try:
        return ArticleStatus(value)
    except ValueError:
        known = [s.value for s in ArticleStatus]
        raise BadRequest(
            [{"loc": ["query", "status"], "msg": f"unknown status; known: {known}"}]
        ) from None


def limit_param(event: BaseProxyEvent) -> int:
    value = event.get_query_string_value("limit")
    if value is None or value == "":
        return DEFAULT_PAGE
    try:
        limit = int(value)
    except ValueError:
        limit = 0
    if not 1 <= limit <= MAX_PAGE:
        raise BadRequest([{"loc": ["query", "limit"], "msg": f"an integer from 1 to {MAX_PAGE}"}])
    return limit


def decode_page_token(token: str | None, status: ArticleStatus) -> dict[str, Any] | None:
    """The ExclusiveStartKey in a next_token from GET /v1/articles, for this status."""
    if not token:
        return None
    try:
        return decode_token(
            token,
            expect_keys=_PAGE_KEYS,
            require_keys={"pk", "gsi1pk"},
            check=lambda key: key["gsi1pk"] == status.value,
            mismatch="next_token belongs to another status",
        )
    except TokenError as err:
        raise BadRequest([{"loc": ["query", "next_token"], "msg": str(err)}]) from None


def execution_link(arn: str | None) -> str | None:
    """The Step Functions console page of an execution (its region from the ARN)."""
    parts = (arn or "").split(":")
    if len(parts) < 7 or parts[0] != "arn":  # noqa: PLR2004 - arn:partition:states:region:...
        return None
    region = parts[3]
    return (
        f"https://{region}.console.aws.amazon.com/states/home?region={region}"
        f"#/v2/executions/details/{arn}"
    )


def queue_attributes(url: str) -> dict[str, str]:
    resp = aws.sqs().get_queue_attributes(
        QueueUrl=url,
        AttributeNames=[
            "QueueArn",
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
        ],
    )
    return {str(k): v for k, v in resp["Attributes"].items()}
