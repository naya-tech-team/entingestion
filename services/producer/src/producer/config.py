"""Runtime configuration read from the Lambda environment."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from functools import lru_cache

# Namespace for deterministic article ids (brief §5). Never change once in use:
# every existing article_id derived from (source, external_id) depends on it.
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


def _content_types_from_env(raw: str | None) -> frozenset[str]:
    if not raw:
        return DEFAULT_ALLOWED_CONTENT_TYPES
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
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
    source_access: str = ""  # JSON: which principals may write/read each source (producer.authz)

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            process_table=env["PROCESS_TABLE"],
            articles_table=env["ARTICLES_TABLE"],
            uploads_table=env["UPLOADS_TABLE"],
            raw_bucket=env["RAW_BUCKET"],
            raw_prefix=env.get("RAW_PREFIX", "raw/"),
            max_file_bytes=int(env.get("MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES)),
            upload_url_ttl_seconds=int(
                env.get("UPLOAD_URL_TTL_SECONDS", DEFAULT_UPLOAD_URL_TTL_SECONDS)
            ),
            allowed_content_types=_content_types_from_env(env.get("ALLOWED_CONTENT_TYPES")),
            source_access=env.get("SOURCE_ACCESS", ""),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
