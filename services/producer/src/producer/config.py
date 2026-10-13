"""Runtime configuration: every environment variable the producer functions read, in one place.

Each function needs only some of the settings. The tables and the bucket are required by all of
them; the rest have defaults or are optional, and a handler that needs an optional one names it
in its REQUIRED tuple and checks it when it builds its dependencies (Settings.require).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import timedelta
from functools import lru_cache

# Namespace for deterministic article ids. Never change once in use: every existing article_id
# derived from (source, external_id) depends on it.
ARTICLE_ID_NAMESPACE = uuid.UUID("8c26b88c-3e6e-4dda-8b6e-f1b097eb99ad")

DEFAULT_ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "text/html",
        "text/plain",
        "text/markdown",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)
DEFAULT_MAX_FILE_BYTES = 5 * 1024**3  # 5 GiB, the S3 single-PUT limit
DEFAULT_UPLOAD_URL_TTL_SECONDS = 900
# Upload rows outlive their ticket by far, so an event redriven from the registrar DLQ (kept
# 14 days) still finds its upload; the janitor settles tickets that were never used.
DEFAULT_UPLOAD_ROW_TTL_DAYS = 15
DEFAULT_FINISHED_UPLOAD_TTL_DAYS = 30  # how long an upload with an outcome stays listed
DEFAULT_TICKET_GRACE_SECONDS = 3600  # a PUT may still be finishing just after its URL expired
DEFAULT_JANITOR_BATCH = 500  # expired tickets the janitor settles per run
DEFAULT_OPEN_PROCESS_IDLE_DAYS = 7  # the janitor closes OPEN processes untouched this long


class MissingSetting(RuntimeError):
    """A required environment variable is not set."""


def _content_types_from_env(raw: str | None) -> frozenset[str]:
    if not raw:
        return DEFAULT_ALLOWED_CONTENT_TYPES
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise MissingSetting(f"{name} is not set")
    return value


@dataclass(frozen=True)
class Settings:
    """Field names are the environment variable names, lower-cased."""

    process_table: str
    articles_table: str
    uploads_table: str
    raw_bucket: str
    raw_prefix: str = "raw/"
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    upload_url_ttl_seconds: int = DEFAULT_UPLOAD_URL_TTL_SECONDS
    allowed_content_types: frozenset[str] = field(
        default_factory=lambda: DEFAULT_ALLOWED_CONTENT_TYPES
    )
    source_access: str = ""  # JSON: which principals may write/read each source (entcommon.authz)
    upload_row_ttl_days: int = DEFAULT_UPLOAD_ROW_TTL_DAYS
    finished_upload_ttl_days: int = DEFAULT_FINISHED_UPLOAD_TTL_DAYS
    # API only: the Powertools idempotency table for Idempotency-Key.
    idempotency_table: str | None = None
    # Janitor only: the registrar's queue (lost S3 events are resent there) and its tunables.
    registrar_queue_url: str | None = None
    open_process_idle_days: int = DEFAULT_OPEN_PROCESS_IDLE_DAYS
    ticket_grace_seconds: int = DEFAULT_TICKET_GRACE_SECONDS
    janitor_batch: int = DEFAULT_JANITOR_BATCH

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        return cls(
            process_table=_required(env, "PROCESS_TABLE"),
            articles_table=_required(env, "ARTICLES_TABLE"),
            uploads_table=_required(env, "UPLOADS_TABLE"),
            raw_bucket=_required(env, "RAW_BUCKET"),
            raw_prefix=env.get("RAW_PREFIX", "raw/"),
            max_file_bytes=int(env.get("MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES)),
            upload_url_ttl_seconds=int(
                env.get("UPLOAD_URL_TTL_SECONDS", DEFAULT_UPLOAD_URL_TTL_SECONDS)
            ),
            allowed_content_types=_content_types_from_env(env.get("ALLOWED_CONTENT_TYPES")),
            source_access=env.get("SOURCE_ACCESS", ""),
            upload_row_ttl_days=int(env.get("UPLOAD_ROW_TTL_DAYS", DEFAULT_UPLOAD_ROW_TTL_DAYS)),
            finished_upload_ttl_days=int(
                env.get("FINISHED_UPLOAD_TTL_DAYS", DEFAULT_FINISHED_UPLOAD_TTL_DAYS)
            ),
            idempotency_table=env.get("IDEMPOTENCY_TABLE") or None,
            registrar_queue_url=env.get("REGISTRAR_QUEUE_URL") or None,
            open_process_idle_days=int(
                env.get("OPEN_PROCESS_IDLE_DAYS", DEFAULT_OPEN_PROCESS_IDLE_DAYS)
            ),
            ticket_grace_seconds=int(env.get("TICKET_GRACE_SECONDS", DEFAULT_TICKET_GRACE_SECONDS)),
            janitor_batch=int(env.get("JANITOR_BATCH", DEFAULT_JANITOR_BATCH)),
        )

    def require(self, *names: str) -> Settings:
        """Raise MissingSetting unless every named environment variable gave a value. Handlers
        call it with their REQUIRED tuple when they build their dependencies."""
        for name in names:
            self.need(name)
        return self

    def need(self, name: str) -> str:
        """The value of an optional setting a function can't run without, by its environment
        variable name; MissingSetting if it wasn't set."""
        attr = name.lower()
        if attr not in {f.name for f in fields(self)}:
            raise ValueError(f"unknown setting {name}")
        value = getattr(self, attr)
        if value is None or value == "":
            raise MissingSetting(f"{name} is not set")
        return str(value)

    @property
    def upload_row_ttl(self) -> timedelta:
        return timedelta(days=self.upload_row_ttl_days)

    @property
    def finished_upload_ttl(self) -> timedelta:
        return timedelta(days=self.finished_upload_ttl_days)

    @property
    def ticket_grace(self) -> timedelta:
        return timedelta(seconds=self.ticket_grace_seconds)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
