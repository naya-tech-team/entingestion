"""Who may use which source (review C3). API Gateway only proves the caller is an IAM principal
with the invoke policy; this decides what that principal may touch.

Each source maps to the principals that may write it (create processes, upload, patch, delete)
and read it, and to the tenant its articles belong to. The tenant is set here, never by the
caller, and travels with the article into ingestion (chunks.tenant_id). Admins may do anything.

SOURCE_ACCESS (JSON, from Terraform):

    {"sources": {"news": {"tenant_id": "t-news", "writers": ["arn:aws:iam::123:role/news-*"],
                          "readers": ["arn:aws:iam::123:role/search-*"]}},
     "admins": ["arn:aws:iam::123:role/entingestion-dev-deploy"]}

Patterns are shell-style (fnmatch) and match the caller's ARN, or for an assumed role also the
role's ARN (arn:aws:iam::<acct>:role/<name>), so policies needn't name sessions. Without a
configuration nobody is allowed anything (fail closed).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

_ASSUMED_ROLE = re.compile(r"^arn:(aws[\w-]*):sts::(\d{12}):assumed-role/([^/]+)/.+$")


@dataclass(frozen=True)
class SourceAccess:
    tenant_id: str
    writers: tuple[str, ...] = ()
    readers: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccessPolicy:
    sources: dict[str, SourceAccess]
    admins: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, raw: str | None) -> AccessPolicy:
        if not raw:
            return cls({})
        data: dict[str, Any] = json.loads(raw)
        sources = {
            name: SourceAccess(
                tenant_id=str(cfg["tenant_id"]),
                writers=tuple(cfg.get("writers") or ()),
                readers=tuple(cfg.get("readers") or ()),
            )
            for name, cfg in (data.get("sources") or {}).items()
        }
        return cls(sources, tuple(data.get("admins") or ()))

    def can_write(self, caller: str, source: str) -> bool:
        cfg = self.sources.get(source)
        return self._admin(caller) or (cfg is not None and _matches(caller, cfg.writers))

    def can_read(self, caller: str, source: str) -> bool:
        cfg = self.sources.get(source)
        return self.can_write(caller, source) or (cfg is not None and _matches(caller, cfg.readers))

    def level(self, caller: str, source: str) -> str | None:
        """The caller's access to the source: write, read, or None."""
        if self.can_write(caller, source):
            return "write"
        return "read" if self.can_read(caller, source) else None

    def tenant_for(self, source: str) -> str | None:
        cfg = self.sources.get(source)
        return cfg.tenant_id if cfg else None

    def _admin(self, caller: str) -> bool:
        return _matches(caller, self.admins)


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
