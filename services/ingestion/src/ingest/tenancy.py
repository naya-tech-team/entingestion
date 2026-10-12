"""Which vector-table registry governs each article (ingest.vectorstore.registry).

An article belongs to its tenant's own registry when the tenant has one (stored, or mapped by
TENANT_PROFILES), else to the default registry (key None). The tenant comes from LATEST
(recorded by Claim). Articles indexed before LATEST recorded it fall back to the producer's row,
looked up in batches, and only when some tenant has its own registry: until then every article
belongs to the default registry anyway.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from ingest import producer
from ingest.status.store import StatusStore
from ingest.vectorstore import registry
from ingest.vectorstore.registry import VectorTables


class Tenancy:
    def __init__(self, store: StatusStore) -> None:
        self.registries: dict[str | None, VectorTables] = registry.load_all(store)
        self.own = frozenset(t for t in self.registries if t is not None)
        self._producer: dict[str, str | None] = {}

    def key(self, tenant: str | None) -> str | None:
        """The registry that governs `tenant`: the tenant itself, or None (the default one)."""
        t = registry.scope(tenant)
        return t if t in self.own else None

    def registry_of(self, tenant: str | None) -> VectorTables:
        return self.registries[self.key(tenant)]

    def tenants(
        self, items: Sequence[dict[str, Any]], *, exact: bool = False
    ) -> dict[str, str | None]:
        """article_id -> tenant for LATEST items. Without `exact`, an item that doesn't record
        its tenant is only looked up when it matters (some tenant has its own registry)."""
        out: dict[str, str | None] = {}
        unknown: list[str] = []
        for item in items:
            article = str(item["pk"])
            if "tenant_id" in item:
                out[article] = str(item["tenant_id"]) or None
            elif not (exact or self.own):
                out[article] = None
            else:
                unknown.append(article)
        missing = [a for a in unknown if a not in self._producer]
        if missing:
            self._producer |= producer.tenants(missing)
        for article in unknown:
            out[article] = self._producer.get(article)
        return out

    def of_tenant(
        self, items: Iterable[dict[str, Any]], tenant: str | None
    ) -> list[dict[str, Any]]:
        """The LATEST items of `tenant`'s articles (whether or not it has its own registry yet:
        a reindex or switch for a tenant gives it one), or, for no tenant, those of the default
        registry."""
        t = registry.scope(tenant)
        if t is None:
            return self.governed(items, None)
        items = list(items)
        tenants = self.tenants(items, exact=True)
        return [i for i in items if registry.scope(tenants[str(i["pk"])]) == t]

    def governed(self, items: Iterable[dict[str, Any]], key: str | None) -> list[dict[str, Any]]:
        """The LATEST items whose article belongs to registry `key` (None: the default one)."""
        items = list(items)
        tenants = self.tenants(items)
        return [i for i in items if self.key(tenants[str(i["pk"])]) == key]
