"""Which vector-table registry governs each article (ingest.vectorstore.registry).

An article belongs to its tenant's own registry when the tenant has one (stored, or mapped by
TENANT_PROFILES), else to the default registry (key None). The tenant comes from LATEST, where
Claim records it; a LATEST without one means the article has no tenant.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from ingest.status.store import StatusStore
from ingest.vectorstore import registry
from ingest.vectorstore.registry import VectorTables


class Tenancy:
    def __init__(self, store: StatusStore) -> None:
        self.registries: dict[str | None, VectorTables] = registry.load_all(store)
        self.own = frozenset(t for t in self.registries if t is not None)

    def key(self, tenant: str | None) -> str | None:
        """The registry that governs `tenant`: the tenant itself, or None (the default one)."""
        t = registry.scope(tenant)
        return t if t in self.own else None

    def registry_of(self, tenant: str | None) -> VectorTables:
        return self.registries[self.key(tenant)]

    @staticmethod
    def tenants(items: Sequence[dict[str, Any]]) -> dict[str, str | None]:
        """article_id -> tenant for LATEST items (None: no tenant)."""
        return {str(i["pk"]): str(i.get("tenant_id") or "") or None for i in items}

    def of_tenant(
        self, items: Iterable[dict[str, Any]], tenant: str | None
    ) -> list[dict[str, Any]]:
        """The LATEST items of `tenant`'s articles (whether or not it has its own registry yet:
        a reindex or switch for a tenant gives it one), or, for no tenant, those of the default
        registry."""
        t = registry.scope(tenant)
        if t is None:
            return self.governed(items, None)
        return [i for i in items if registry.scope(i.get("tenant_id")) == t]

    def governed(self, items: Iterable[dict[str, Any]], key: str | None) -> list[dict[str, Any]]:
        """The LATEST items whose article belongs to registry `key` (None: the default one)."""
        return [i for i in items if self.key(i.get("tenant_id")) == key]
