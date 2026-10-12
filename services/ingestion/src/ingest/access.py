"""Who may search which tenant, and each source's PII policy: the producer's per-source access
policy (producer.authz), read from the SSM parameter the producer stack publishes:

    {"sources": {"news": {"tenant_id": "t-news", "writers": ["arn:aws:iam::123:role/news-*"],
                          "readers": ["arn:aws:iam::123:role/search-*"], "pii": "redact"}},
     "admins": ["arn:aws:iam::123:role/entingestion-dev-deploy"]}

A caller may search a tenant if it reads or writes some source of that tenant, or is an admin.
Patterns are shell-style and match the caller's ARN or, for an assumed role, the role's ARN, as
in the producer (a small copy of its matching: the services share no code). No parameter, or
an unreadable one, allows nothing (fail closed).

pii (Normalize, when PII screening is on): "block" (don't index), "redact" (replace entities
with [TYPE]) or "allow" (the default: index as is, record what was found).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Literal

from ingest import aws
from ingest.config import get_settings

PiiPolicy = Literal["block", "redact", "allow"]
_PII = ("block", "redact", "allow")
_ASSUMED_ROLE = re.compile(r"^arn:(aws[\w-]*):sts::(\d{12}):assumed-role/([^/]+)/.+$")
CACHE_SECONDS = 300


@dataclass(frozen=True)
class SourceAccess:
    tenant_id: str
    writers: tuple[str, ...] = ()
    readers: tuple[str, ...] = ()
    pii: PiiPolicy = "allow"


@dataclass(frozen=True)
class AccessPolicy:
    sources: dict[str, SourceAccess]
    admins: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, raw: str | None) -> AccessPolicy:
        if not raw:
            return cls({})
        data: dict[str, Any] = json.loads(raw)
        sources = {}
        for name, cfg in (data.get("sources") or {}).items():
            pii = str(cfg.get("pii") or "allow")
            sources[name] = SourceAccess(
                tenant_id=str(cfg["tenant_id"]),
                writers=tuple(cfg.get("writers") or ()),
                readers=tuple(cfg.get("readers") or ()),
                pii=pii if pii in _PII else "block",  # type: ignore[arg-type]  # unknown: safest
            )
        return cls(sources, tuple(data.get("admins") or ()))

    def can_search(self, caller: str, tenant_id: str) -> bool:
        if _matches(caller, self.admins):
            return True
        return any(
            cfg.tenant_id == tenant_id and _matches(caller, cfg.writers + cfg.readers)
            for cfg in self.sources.values()
        )

    def pii_for(self, source: str | None) -> PiiPolicy:
        cfg = self.sources.get(source or "")
        return cfg.pii if cfg else "allow"


def identities(caller: str) -> list[str]:
    """The caller's ARN, plus its role's ARN when the caller is an assumed-role session."""
    found = [caller]
    match = _ASSUMED_ROLE.match(caller)
    if match:
        partition, account, role = match.groups()
        found.append(f"arn:{partition}:iam::{account}:role/{role}")
    return found


def _matches(caller: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(arn, p) for arn in identities(caller) for p in patterns)


_cache: tuple[float, AccessPolicy] | None = None


def policy() -> AccessPolicy:
    """The current policy, re-read from SSM at most every CACHE_SECONDS per warm Lambda."""
    global _cache  # noqa: PLW0603 - per-container cache of the SSM parameter
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < CACHE_SECONDS:
        return _cache[1]
    name = get_settings().source_access_parameter
    raw = aws.ssm().get_parameter(Name=name)["Parameter"]["Value"] if name else None
    _cache = (now, AccessPolicy.from_json(raw))
    return _cache[1]


def reset() -> None:
    """For tests: forget the cached policy."""
    global _cache  # noqa: PLW0603
    _cache = None
