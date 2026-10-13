"""Who may use which source. API Gateway (AWS_IAM) only proves the caller is an IAM principal with
the invoke policy; this decides what that principal may touch.

Each source maps to the principals that may write it and read it, to the tenant its articles
belong to, and to its PII policy. Admins may do anything. The policy is JSON, published by the
producer stack (SOURCE_ACCESS, and an SSM parameter for ingestion):

    {"sources": {"news": {"tenant_id": "t-news", "writers": ["arn:aws:iam::123:role/news-*"],
                          "readers": ["arn:aws:iam::123:role/search-*"], "pii": "redact"}},
     "admins": ["arn:aws:iam::123:role/entingestion-dev-deploy"]}

Patterns are shell-style (fnmatch, case-sensitive) and match the caller's ARN or, for an
assumed-role session, also the role's ARN (arn:<partition>:iam::<acct>:role/<name>), so policies
needn't name sessions. Without a configuration nobody is allowed anything (fail closed).

pii: "block" (don't index), "redact" (replace entities with [TYPE]) or "allow" (the default:
index as is, record what was found). An unknown value is treated as "block", the safest.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Literal

PiiPolicy = Literal["block", "redact", "allow"]
Level = Literal["write", "read"]
PII_POLICIES: tuple[PiiPolicy, ...] = ("block", "redact", "allow")

_ASSUMED_ROLE = re.compile(r"^arn:(aws[\w-]*):sts::(\d{12}):assumed-role/([^/]+)/.+$")
_RELATIVE = re.compile(r"^(user|role)/.+$")


def identities(caller: str) -> list[str]:
    """The caller's ARN, plus its role's ARN when the caller is an assumed-role session."""
    found = [caller]
    match = _ASSUMED_ROLE.match(caller)
    if match:
        partition, account, role = match.groups()
        found.append(f"arn:{partition}:iam::{account}:role/{role}")
    return found


def matches(caller: str, patterns: Iterable[str]) -> bool:
    """Whether any of the caller's identities matches any pattern (shell-style)."""
    pats = tuple(patterns)
    return any(fnmatchcase(arn, p) for arn in identities(caller) for p in pats)


def expand_principal(principal: str, account_id: str, partition: str = "aws") -> str:
    """A principal relative to the account ("role/<name>", "user/<name>", patterns allowed) as a
    full IAM ARN; a full ARN is returned unchanged."""
    if principal.startswith("arn:"):
        return principal
    if not _RELATIVE.match(principal):
        raise ValueError(f'principal must be "role/<name>", "user/<name>" or an ARN: {principal!r}')
    return f"arn:{partition}:iam::{account_id}:{principal}"


def _pii(value: Any) -> PiiPolicy:
    pii = str(value or "allow")
    for known in PII_POLICIES:
        if pii == known:
            return known
    return "block"  # unknown: safest


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
        """Parse the policy; None or "" gives the empty policy (nobody may do anything)."""
        if not raw:
            return cls({})
        data: dict[str, Any] = json.loads(raw)
        sources = {
            name: SourceAccess(
                tenant_id=str(cfg["tenant_id"]),
                writers=tuple(cfg.get("writers") or ()),
                readers=tuple(cfg.get("readers") or ()),
                pii=_pii(cfg.get("pii")),
            )
            for name, cfg in (data.get("sources") or {}).items()
        }
        return cls(sources, tuple(data.get("admins") or ()))

    def is_admin(self, caller: str) -> bool:
        return matches(caller, self.admins)

    def can_write(self, caller: str, source: str) -> bool:
        cfg = self.sources.get(source)
        return self.is_admin(caller) or (cfg is not None and matches(caller, cfg.writers))

    def can_read(self, caller: str, source: str) -> bool:
        """Readers, writers and admins may read."""
        cfg = self.sources.get(source)
        return self.can_write(caller, source) or (cfg is not None and matches(caller, cfg.readers))

    def level(self, caller: str, source: str) -> Level | None:
        """The caller's access to the source: write, read, or None."""
        if self.can_write(caller, source):
            return "write"
        return "read" if self.can_read(caller, source) else None

    def tenant_for(self, source: str) -> str | None:
        cfg = self.sources.get(source)
        return cfg.tenant_id if cfg else None

    def can_search(self, caller: str, tenant_id: str) -> bool:
        """A caller may search a tenant if it reads or writes some source of that tenant, or is
        an admin."""
        if self.is_admin(caller):
            return True
        return any(
            cfg.tenant_id == tenant_id and matches(caller, cfg.writers + cfg.readers)
            for cfg in self.sources.values()
        )

    def pii_for(self, source: str | None) -> PiiPolicy:
        """The source's PII policy; "allow" for an unknown or missing source."""
        cfg = self.sources.get(source or "")
        return cfg.pii if cfg else "allow"
