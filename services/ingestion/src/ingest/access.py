"""Who may search which tenant, and each source's PII policy: the producer's per-source access
policy (entcommon.authz), read from the SSM parameter the producer stack publishes:

    {"sources": {"news": {"tenant_id": "t-news", "writers": ["arn:aws:iam::123:role/news-*"],
                          "readers": ["arn:aws:iam::123:role/search-*"], "pii": "redact"}},
     "admins": ["arn:aws:iam::123:role/entingestion-dev-deploy"]}

A caller may search a tenant if it reads or writes some source of that tenant, or is an admin.
Patterns are shell-style and match the caller's ARN or, for an assumed role, the role's ARN. No
parameter, or an unreadable one, allows nothing (fail closed).

pii (Normalize, when PII screening is on): "block" (don't index), "redact" (replace entities
with [TYPE]) or "allow" (the default: index as is, record what was found).
"""

from __future__ import annotations

import time

from entcommon.authz import AccessPolicy, PiiPolicy, SourceAccess, identities

from ingest import aws
from ingest.config import get_settings

__all__ = ["AccessPolicy", "PiiPolicy", "SourceAccess", "identities", "policy", "reset"]

_cache: tuple[float, AccessPolicy] | None = None


def policy() -> AccessPolicy:
    """The current policy, re-read from SSM at most every ACCESS_CACHE_SECONDS per warm
    Lambda."""
    global _cache  # noqa: PLW0603 - per-container cache of the SSM parameter
    settings = get_settings()
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < settings.access.cache_seconds:
        return _cache[1]
    name = settings.access.parameter
    raw = aws.ssm().get_parameter(Name=name)["Parameter"]["Value"] if name else None
    _cache = (now, AccessPolicy.from_json(raw))
    return _cache[1]


def reset() -> None:
    """For tests: forget the cached policy."""
    global _cache  # noqa: PLW0603
    _cache = None
