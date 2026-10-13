"""API request and response models."""

from __future__ import annotations

import json
import re
import unicodedata
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

MAX_METADATA_PAIRS = 50

SOURCE_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"

MAX_METADATA_BYTES = 16 * 1024  # serialized; keeps every item well inside DynamoDB's 400 KB
# Set by the service, never by callers: the tenant comes from the source's access policy.
RESERVED_METADATA_KEYS = frozenset({"tenant_id"})


def _check_metadata(value: dict[str, str]) -> dict[str, str]:
    reserved = sorted(RESERVED_METADATA_KEYS & value.keys())
    if reserved:
        raise ValueError(f"reserved metadata keys: {', '.join(reserved)}")
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_METADATA_BYTES:
        raise ValueError(f"metadata must be at most {MAX_METADATA_BYTES} bytes as JSON")
    return value


MetadataKey = Annotated[str, StringConstraints(min_length=1, max_length=128)]
MetadataValue = Annotated[str, StringConstraints(max_length=2048)]
Metadata = Annotated[
    dict[MetadataKey, MetadataValue],
    Field(max_length=MAX_METADATA_PAIRS),
    AfterValidator(_check_metadata),
]


class ProcessStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class UploadStatus(StrEnum):
    PENDING = "PENDING"
    REGISTERING = "REGISTERING"
    REGISTERED = "REGISTERED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"  # the ticket expired and no object arrived (janitor)


class ArticleStatus(StrEnum):
    STAGED = "STAGED"


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class _Response(BaseModel):
    # Stored items may carry bookkeeping attributes the API doesn't expose.
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class CreateProcessRequest(_Request):
    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    source: Annotated[str, StringConstraints(pattern=SOURCE_PATTERN)]
    expected_count: Annotated[int, Field(ge=0)] | None = None
    metadata: Metadata = Field(default_factory=dict)


class CloseProcessRequest(_Request):
    expected_count: Annotated[int, Field(ge=0)] | None = None


def _is_safe_filename(name: str) -> bool:
    if name in {".", ".."}:
        return False
    if "/" in name or "\\" in name:
        return False
    return not any(unicodedata.category(ch).startswith("C") for ch in name)


class CreateUploadRequest(_Request):
    filename: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    content_type: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    size_bytes: Annotated[int, Field(gt=0)]
    sha256: str
    external_id: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None = None
    title: Annotated[str, StringConstraints(max_length=1000)] | None = None
    metadata: Metadata | None = None

    @field_validator("filename")
    @classmethod
    def _filename_is_a_plain_name(cls, value: str) -> str:
        if not _is_safe_filename(value):
            raise ValueError("filename must not contain path separators or control characters")
        return value

    @field_validator("sha256", mode="before")
    @classmethod
    def _sha256_is_hex(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.lower()
            if not re.fullmatch(SHA256_PATTERN, value):
                raise ValueError("sha256 must be 64 hex characters")
        return value


class PatchArticleRequest(_Request):
    title: Annotated[str, StringConstraints(max_length=1000)] | None = None
    metadata: Metadata | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> PatchArticleRequest:
        if not self.model_fields_set & {"title", "metadata"}:
            raise ValueError("at least one of title or metadata is required")
        return self


class ListArticlesQuery(_Request):
    limit: Annotated[int, Field(ge=1, le=500)] = 100
    next_token: str | None = None


class ListUploadsQuery(_Request):
    limit: Annotated[int, Field(ge=1, le=500)] = 100
    next_token: str | None = None


class ListProcessesQuery(_Request):
    source: Annotated[str, StringConstraints(pattern=SOURCE_PATTERN)]
    limit: Annotated[int, Field(ge=1, le=500)] = 100
    next_token: str | None = None


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class Process(_Response):
    process_id: str
    name: str
    source: str
    status: ProcessStatus
    expected_count: int | None = None  # uploads the client meant to send
    registered_count: int = 0  # distinct articles registered
    rejected_count: int = 0  # uploads rejected
    superseded_count: int = 0  # uploads a newer upload of the same article overtook
    expired_count: int = 0  # tickets never used (no object arrived)
    tickets_issued: int = 0  # uploads requested
    pending_uploads: int = 0  # tickets not yet registered, rejected, superseded or expired
    metadata: dict[str, str] = Field(default_factory=dict)
    created_by: str
    created_at: str
    updated_at: str
    closed_at: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def settled(self) -> bool:
        return is_settled(
            {
                "status": self.status,
                "expected_count": self.expected_count,
                "tickets_issued": self.tickets_issued,
                "pending_uploads": self.pending_uploads,
            }
        )


def is_settled(process: dict[str, Any]) -> bool:
    """CLOSED, and every upload has an outcome: no ticket is still pending and at least
    expected_count tickets were issued. Ingestion applies the same rule."""
    expected = process.get("expected_count")
    if process.get("status") != ProcessStatus.CLOSED or expected is None:
        return False
    pending = int(process.get("pending_uploads") or 0)
    return pending <= 0 and int(process.get("tickets_issued") or 0) >= int(expected)


class UploadTicket(_Response):
    upload_id: str
    article_id: str
    process_id: str
    method: str = "PUT"
    url: str
    headers: dict[str, str]
    expires_at: str


class Upload(_Response):
    upload_id: str
    article_id: str
    process_id: str
    source: str
    s3_key: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    external_id: str | None = None
    title: str | None = None
    metadata: dict[str, str] | None = None
    status: UploadStatus
    reason: str | None = None
    requested_by: str
    created_at: str
    finished_at: str | None = None


class UploadSummary(_Response):
    """An upload as listed under its process (GET /v1/processes/{id}/uploads)."""

    upload_id: str
    article_id: str
    filename: str
    content_type: str
    size_bytes: int
    status: UploadStatus
    reason: str | None = None
    title: str | None = None
    external_id: str | None = None
    created_at: str
    finished_at: str | None = None


class UploadPage(_Response):
    items: list[UploadSummary]
    next_token: str | None = None


class Access(StrEnum):
    READ = "read"
    WRITE = "write"


class SourceGrant(_Response):
    source: str
    tenant_id: str
    access: Access


class SourceList(_Response):
    items: list[SourceGrant]


class Article(_Response):
    article_id: str
    process_id: str
    first_process_id: str
    source: str
    external_id: str | None = None
    title: str | None = None
    filename: str
    content_type: str
    s3_uri: str
    s3_key: str
    s3_version_id: str | None = None
    content_sha256: str
    size_bytes: int
    upload_id: str
    status: ArticleStatus
    metadata: dict[str, str] = Field(default_factory=dict)
    tenant_id: str | None = None
    created_at: str
    updated_at: str
    updated_date: str


class ArticlePage(_Response):
    items: list[Article]
    next_token: str | None = None


class ProcessPage(_Response):
    items: list[Process]
    next_token: str | None = None
