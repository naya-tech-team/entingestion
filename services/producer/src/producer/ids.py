"""Identifier generation (brief §5)."""

from __future__ import annotations

import uuid

from ulid import ULID

from producer.config import ARTICLE_ID_NAMESPACE

PROCESS_PREFIX = "prc_"
UPLOAD_PREFIX = "upl_"
ARTICLE_PREFIX = "art_"
_UNIT_SEPARATOR = "\x1f"


def new_process_id() -> str:
    return f"{PROCESS_PREFIX}{ULID()}"


def new_upload_id() -> str:
    return f"{UPLOAD_PREFIX}{ULID()}"


def article_id_for(source: str, external_id: str | None) -> str:
    """Deterministic id when external_id is given, so re-sends update the same article."""
    if external_id is None:
        return f"{ARTICLE_PREFIX}{ULID()}"
    name = f"{source}{_UNIT_SEPARATOR}{external_id}"
    return f"{ARTICLE_PREFIX}{uuid.uuid5(ARTICLE_ID_NAMESPACE, name).hex}"
