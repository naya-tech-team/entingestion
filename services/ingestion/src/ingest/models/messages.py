"""Queue message for the ingest and backfill queues (brief §4.2)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ingest.models.status import StepName

Trigger = Literal["STREAM", "RECONCILER", "MANUAL", "BACKFILL"]
Priority = Literal["normal", "low"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class IngestMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    article_id: str = Field(min_length=1, max_length=64)
    process_id: str | None = None
    event: Literal["UPSERT", "REMOVE"]
    s3_uri: str | None = None
    s3_version_id: str | None = None
    content_sha256: str | None = None  # from the producer row; no S3 HEAD needed
    # Fingerprints of the row's title + metadata + tenant, and of the title alone
    # (ids.meta_sha256, ids.title_sha256): a change to the metadata only is applied in place
    # (Claim's METADATA action); a new title is a content change (it's in every embed_text).
    meta_sha256: str | None = None
    title_sha256: str | None = None
    # The producer row's updated_at. Distinguishes re-registrations and PATCHes of unchanged
    # content (each is its own run, brief UC3) and feeds the freshness-lag metric.
    source_updated_at: str | None = None
    trigger: Trigger
    requested_by: str | None = None
    force: bool = False
    from_stage: StepName | None = None
    priority: Priority = "normal"
    # The producer row's tenant: picks the tenant's vector-table registry (which tables a live
    # run writes, ingest.vectorstore.registry). None = the default registry.
    tenant_id: str | None = Field(default=None, min_length=1, max_length=128)
    # Reindex into another vector table (brief §7.3); None = the configured live table.
    target_table: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,62}$")

    @field_validator("content_sha256", "meta_sha256", "title_sha256")
    @classmethod
    def _sha256_hex(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.match(value):
            raise ValueError("must be 64 lowercase hex characters")
        return value

    @model_validator(mode="after")
    def _upsert_has_source(self) -> IngestMessage:
        if self.event == "UPSERT":
            missing = [
                name
                for name in ("process_id", "s3_uri", "content_sha256")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"UPSERT requires {', '.join(missing)}")
            if self.s3_uri is not None and not self.s3_uri.startswith("s3://"):
                raise ValueError("s3_uri must start with s3://")
        if self.from_stage is not None and self.event == "REMOVE":
            raise ValueError("from_stage only applies to UPSERT")
        return self

    @property
    def is_replay(self) -> bool:
        return self.force or self.from_stage is not None
