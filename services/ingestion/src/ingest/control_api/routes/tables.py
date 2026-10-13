"""Vector tables: reindex into one, see them, switch the live one, retire one.

    POST /v1/reindex                     reindex/rebuild into a vector table (state machine)
    GET  /v1/tables                      live table, write tables, row counts per table
    POST /v1/tables/active               switch the live table (go live / roll back), gated
    DELETE /v1/tables/writes/{table}     stop writing a table (retire it, or cancel a change)

Each takes an optional tenant_id: a tenant with its own vector-table registry
(ingest.vectorstore.registry) moves to another model on its own. Without one they act on the
default registry, whose live table chunks_active also serves.
"""

from __future__ import annotations

import json
from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import Response
from aws_lambda_powertools.event_handler.api_gateway import Router
from pydantic import Field

from ingest import aws, evaluation
from ingest.config import get_settings
from ingest.control_api.http import (
    MAX_TENANT_ID,
    BadRequest,
    Request,
    body,
    caller,
    execution_link,
    json_response,
    tenant_param,
)
from ingest.ids import new_run_id
from ingest.models.status import ArticleStatus, StepName
from ingest.reindex import ReindexFilter, ReindexRequest, plan_items
from ingest.status.store import StatusStore
from ingest.tenancy import Tenancy
from ingest.vectorstore import registry
from ingest.vectorstore.targets import TARGETS

logger = Logger(child=True)
router = Router()  # type: ignore[no-untyped-call]


class ReindexBody(Request):
    filter: ReindexFilter = Field(default_factory=ReindexFilter)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=MAX_TENANT_ID)
    target_table: str | None = None
    from_stage: StepName = StepName.EMBED
    dry_run: bool = False


class ActivateBody(Request):
    table: str
    tenant_id: str | None = Field(default=None, min_length=1, max_length=MAX_TENANT_ID)
    force: bool = False  # switch despite missing articles or a weaker (or no) evaluation


@router.post("/v1/reindex")
def reindex() -> Response[str]:
    """Re-run indexed articles into a vector table: from_stage EMBED re-embeds into a new table
    (model change), INDEX reloads staged vectors (rebuild, no embedding calls)."""
    req = body(router.current_event, ReindexBody)
    by = caller(router.current_event)
    store = aws.status_store()
    tenant = registry.scope(req.tenant_id)
    request = ReindexRequest(
        filter=req.filter,
        target_table=req.target_table or registry.load(store, tenant).live,
        from_stage=req.from_stage,
        requested_by=by,
        tenant_id=tenant,
    )
    problems = request.check()
    if problems:
        raise BadRequest([{"loc": ["body"], "msg": p} for p in problems])
    count = len(plan_items(store, request.filter, request.target_table, tenant))
    out: dict[str, Any] = {
        "dry_run": req.dry_run,
        "count": count,
        "tenant_id": tenant,
        "target_table": request.target_table,
        "from_stage": request.from_stage.value,
    }
    if not req.dry_run:
        # The tenant's live runs write the table from now on, so edits made while it fills
        # aren't missed (a tenant's first change gives it its own registry).
        out["tables"] = registry.add_write(store, request.target_table, by, tenant).as_dict()
        name = f"reindex-{request.target_table}-{new_run_id()}"[:80]
        resp = aws.sfn().start_execution(
            stateMachineArn=get_settings().reindex_state_machine_arn,
            name=name,
            input=request.model_dump_json(),
        )
        out["execution"] = execution_link(resp["executionArn"])
        out["execution_arn"] = resp["executionArn"]
        logger.info("reindex started", extra=out)
    return json_response(200 if req.dry_run else 202, out)


@router.get("/v1/tables")
def tables() -> Response[str]:
    """Which vector table chunks_active serves, the registry (live table and write tables) of
    the tenant (?tenant_id=; default: the default registry), row counts, every tenant's own
    registry, and the embedding profiles."""
    tenant = tenant_param(router.current_event)
    out = _tables({"action": "status"})
    store = aws.status_store()
    current = registry.load(store, tenant)
    out["tenant_id"] = tenant
    out["registry"] = current.as_dict()
    # chunks_active serves the default registry's live table; a tenant reads its table directly.
    out["consistent"] = (
        out.get("active_table") == current.live
        if current.tenant is None
        else (out.get("tables") or {}).get(current.live) is not None
    )
    out["tenants"] = {t: r.as_dict() for t, r in registry.load_all(store).items() if t is not None}
    out["profiles"] = {
        name: {
            "table": p.table,
            "provider": p.provider,
            "model": p.model,
            "dimension": p.dimension,
        }
        for name, p in get_settings().embedding.profiles.items()
    }
    return json_response(200, out)


@router.post("/v1/tables/active")
def activate_table() -> Response[str]:
    """Switch chunks_active to another table (go live after a reindex, or roll back).

    Refused (409) unless live runs write the table, every indexed article's current version is
    in it, and its newest evaluation (at most EVAL_MAX_AGE_DAYS old) has recall@10 no more than
    EVAL_TOLERANCE below the live table's on the same golden set; force overrides the coverage
    and evaluation checks (a rollback to an old table usually needs it)."""
    req = body(router.current_event, ActivateBody)
    by = caller(router.current_event)
    if req.table not in TARGETS:
        raise BadRequest([{"loc": ["body", "table"], "msg": f"known tables: {sorted(TARGETS)}"}])
    store = aws.status_store()
    tenant = registry.scope(req.tenant_id)
    current = registry.load(store, tenant)
    if req.table not in current.writes:
        return json_response(
            409,
            {
                "message": f"{req.table} isn't written by live runs; reindex into it first",
                "tables": current.as_dict(),
            },
        )
    missing = coverage_gaps(store, req.table, tenant)
    if missing and not req.force:
        return json_response(
            409,
            {
                "message": f"{len(missing)} indexed articles aren't in {req.table} yet",
                "missing": len(missing),
                "sample": missing[:20],
                "hint": "reindex with filter.only_uncovered, or wait for the drift check",
            },
        )
    if not req.force:
        gate = get_settings().evaluation
        refused = evaluation.switch_gate(
            req.table, current.live, tolerance=gate.tolerance, max_age_days=gate.max_age_days
        )
        if refused is not None:
            return json_response(409, refused | {"gate": "evaluation"})
    if current.tenant is None and tenant is None:
        out = _tables({"action": "activate", "table": req.table})  # chunks_active follows
    else:  # a tenant's own registry: its searches read the table directly
        out = {"previous": current.live, "active_table": req.table}
    out["tenant_id"] = tenant
    out["tables"] = registry.set_live(store, req.table, by, tenant).as_dict()
    logger.warning("active table switched", extra=out | {"by": by, "forced": req.force})
    return json_response(200, out)


@router.delete("/v1/tables/writes/<table>")
def retire_table(table: str) -> Response[str]:
    """Stop writing a table: retire the old table after a switch, or cancel a model change.
    ?tenant_id= for a tenant's own registry. The table's rows stay (a later rollback)."""
    tenant = tenant_param(router.current_event)
    try:
        out = registry.remove_write(aws.status_store(), table, caller(router.current_event), tenant)
    except registry.RegistryError as err:
        return json_response(409, {"message": str(err)})
    logger.warning("write table retired", extra={"table": table, "tenant": tenant})
    return json_response(200, out.as_dict())


def coverage_gaps(store: StatusStore, table: str, tenant: str | None = None) -> list[str]:
    """Indexed articles of `tenant` (None: of the default registry) whose current version isn't
    in `table` yet."""
    attr = registry.version_attr(table)
    indexed = store.by_status(ArticleStatus.INDEXED)
    gaps = [i for i in indexed if i.get(attr) != i.get("doc_version")]
    return [str(i["pk"]) for i in Tenancy(store).of_tenant(gaps, tenant)]


def _tables(payload: dict[str, Any]) -> dict[str, Any]:
    """Vector-table operations run in the tables function (inside the VPC, as ingest_ops)."""
    resp = aws.lambda_client().invoke(
        FunctionName=get_settings().tables_function_name, Payload=json.dumps(payload).encode()
    )
    out = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        raise RuntimeError(f"tables {payload['action']} failed: {out.get('errorMessage', out)}")
    return dict(out)
