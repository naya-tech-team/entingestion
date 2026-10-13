"""Which vector tables runs write, and which one search reads, per tenant.

Small items in ingestion_status:

    #CONFIG/VECTOR_TABLES            the default registry: every tenant without its own, and
                                     articles without a tenant ('default')
    #CONFIG/VECTOR_TABLES#<tenant>   a tenant's own registry

each with

    live    the table the tenant's searches read (for the default registry also the table the
            chunks_active view serves: the switch sets both)
    writes  every table a live run of the tenant writes, live first. During a model change the
            new table is added here before its reindex starts, so edits made while it fills land
            in both tables; the old table stays here after the switch, so a rollback is exact,
            until it's retired.
    epoch   bumped on every change; changes are conditional on it

A tenant has its own registry once one is stored for it (its first change: a reindex, switch or
retire with its tenant_id), or when TENANT_PROFILES maps it to a profile: then, until its first
change, its registry is {live: chunks_<profile>, writes: [chunks_<profile>]}. Every other tenant
uses the default registry, which without its item is the default profile's table alone.

Kept in DynamoDB, not Postgres, because the steps that need it run outside the VPC.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError
from entcommon.dynamo import is_condition_failure
from entcommon.timeutil import iso_now

from ingest.errors import ErrorCode, make_error
from ingest.status import keys
from ingest.vectorstore.targets import TARGETS, profile_table

if TYPE_CHECKING:
    # The reads part of the status store is all a registry needs (the store passes itself).
    from ingest.status.reads import ReadsMixin as StatusReads

NAME = "VECTOR_TABLES"
DEFAULT_TENANT = "default"  # articles without a tenant (ingest.vectorstore.index)


class RegistryError(ValueError):
    """A change the registry refuses (unknown table, retiring the live table, ...)."""


def scope(tenant: str | None) -> str | None:
    """The registry key of a tenant name: None for the default registry."""
    return None if not tenant or tenant == DEFAULT_TENANT else tenant


def config_name(tenant: str | None) -> str:
    t = scope(tenant)
    return NAME if t is None else f"{NAME}#{t}"


@dataclass(frozen=True)
class VectorTables:
    live: str
    writes: tuple[str, ...]
    epoch: int
    tenant: str | None = None  # whose registry: None = the default one

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"live": self.live, "writes": list(self.writes), "epoch": self.epoch}
        if self.tenant is not None:
            out["tenant_id"] = self.tenant
        return out


def _from_item(item: dict[str, Any], tenant: str | None) -> VectorTables:
    live = str(item["live"])
    writes = [str(t) for t in item.get("writes") or [] if t != live]
    return VectorTables(live, (live, *writes), int(item.get("epoch", 0)), tenant)


def load_default(store: StatusReads) -> VectorTables:
    item = store.get_config(NAME)
    if not item:
        live = store.settings.target_table
        return VectorTables(live, (live,), 0)
    return _from_item(item, None)


def recorded_live(store: StatusReads) -> str | None:
    """The default registry's live table if a change recorded one, else None (a new environment,
    where chunks_active serves the default profile's table). migrate reads it, so a rebuilt
    database serves the same table as the registry."""
    item = store.get_config(NAME)
    return str(item["live"]) if item else None


def load(store: StatusReads, tenant: str | None = None) -> VectorTables:
    """The registry that governs `tenant`: its own, else the default one (tenant None)."""
    t = scope(tenant)
    if t is not None:
        item = store.get_config(config_name(t))
        if item:
            return _from_item(item, t)
        profile = store.settings.embedding.tenants.get(t)
        if profile:
            table = profile_table(profile)
            return VectorTables(table, (table,), 0, t)
    return load_default(store)


def load_all(store: StatusReads) -> dict[str | None, VectorTables]:
    """Every registry: the default one (key None) and each tenant's own."""
    out: dict[str | None, VectorTables] = {None: load_default(store)}
    for item in store.configs(f"{NAME}#"):
        tenant = str(item["sk"]).removeprefix(f"{NAME}#")
        out[tenant] = _from_item(item, tenant)
    for tenant in store.settings.embedding.tenants:
        if scope(tenant) is not None and tenant not in out:
            out[tenant] = load(store, tenant)
    return out


def _save(store: StatusReads, current: VectorTables, new: VectorTables, by: str) -> VectorTables:
    """Store `new` for its tenant. A tenant's first own registry (copied from the default one or
    from TENANT_PROFILES) must not exist yet; later changes need the epoch they started from."""
    condition: dict[str, Any] = {"ConditionExpression": "attribute_not_exists(pk)"}
    if current.tenant == new.tenant:
        condition = {
            "ConditionExpression": "attribute_not_exists(pk) OR epoch = :seen",
            "ExpressionAttributeValues": {":seen": current.epoch},
        }
    try:
        store.table.put_item(
            Item={
                **keys.config_key(config_name(new.tenant)),
                **new.as_dict(),
                "updated_at": iso_now(),
                "updated_by": by,
            },
            **condition,
        )
    except ClientError as err:
        if is_condition_failure(err):
            raise make_error(ErrorCode.DB_CONFLICT, "vector tables changed concurrently") from err
        raise
    return new


def _known(table: str) -> None:
    if table not in TARGETS:
        raise RegistryError(f"unknown table {table!r}; known: {sorted(TARGETS)}")


def add_write(store: StatusReads, table: str, by: str, tenant: str | None = None) -> VectorTables:
    """Start writing `table` from the tenant's live runs too (before its reindex)."""
    _known(table)
    current = load(store, tenant)
    owner = scope(tenant)
    if table in current.writes and current.tenant == owner:
        return current
    writes = current.writes if table in current.writes else (*current.writes, table)
    new = VectorTables(current.live, writes, current.epoch + 1, owner)
    return _save(store, current, new, by)


def remove_write(
    store: StatusReads, table: str, by: str, tenant: str | None = None
) -> VectorTables:
    """Stop writing `table` for the tenant (retire an old table, or cancel a model change)."""
    current = load(store, tenant)
    owner = scope(tenant)
    if table == current.live:
        raise RegistryError(f"{table} is live; switch to another table first")
    if table not in current.writes:
        return current
    writes = tuple(t for t in current.writes if t != table)
    return _save(store, current, VectorTables(current.live, writes, current.epoch + 1, owner), by)


def set_live(store: StatusReads, table: str, by: str, tenant: str | None = None) -> VectorTables:
    """Record the switch (for the default registry: after chunks_active points at `table`). The
    old live table stays written, so switching back is exact."""
    current = load(store, tenant)
    if table not in current.writes:
        raise RegistryError(f"{table} isn't written by live runs; add it and reindex first")
    writes = (table, *(t for t in current.writes if t != table))
    new = VectorTables(table, writes, current.epoch + 1, scope(tenant))
    return _save(store, current, new, by)


def version_attr(table: str) -> str:
    """LATEST attribute holding the doc_version last written to `table`."""
    return f"tv_{table}"
