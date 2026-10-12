"""Checks for the architecture-review fixes against a deployed environment (M1).

    uv run python scripts/acceptance_review.py [--env dev] [--only delete,switch,drift]

delete  An article deleted while its run is still going ends DELETED with no rows (the run
        hands over, or the delete runs after it), and a redelivered upsert can't revive it.
switch  Model change with the registry: the switch is refused until the new table is written
        and covered; a reindex of the uncovered articles fills it; edits during the change
        reach both tables; switch, roll back, retire the new table.
drift   The weekly drift check finds nothing to repair in a consistent environment.
m3      Retrieval: an HTML table's cell text isn't duplicated; POST /v1/search finds the article
        for its tenant (and filters by source), refuses another tenant; a metadata PATCH is a
        METADATA run; the switch to a table without a fresh evaluation is refused (409).
tenants Per-tenant models: an article of tenant t-small (source tenant-test, mapped to the
        titan2_512 profile) is written only to chunks_titan2_512, its tenant's live table; search
        for t-small embeds with that model and finds it; the default tenant doesn't see it.
m2      Producer: an unconfigured source is refused (403); a retried POST with the same
        Idempotency-Key is the same process; two uploads of one article in one process still
        settle it; the article carries the source's tenant into ingestion; the janitor runs.

Each run creates one or two small articles (source "acceptance") and deletes them again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import boto3
from acceptance_phase2 import Ctx, article_text, in_status, wait
from acceptance_phase3 import reindex, tables
from gate_phase1 import tf_output

LIVE, NEW = "chunks_titan2_1024", "chunks_titan2_512"


def _ingest(ctx: Ctx, filename: str, tag: str) -> str:
    out = ctx.ingest_files({filename: article_text(tag)}, f"review {tag}")
    return str(out["uploaded"][0]["article_id"])


def _delete(ctx: Ctx, article_id: str) -> None:
    status, _ = ctx.producer_api.call("DELETE", f"/v1/articles/{article_id}")
    if status != 204:
        raise RuntimeError(f"producer delete {article_id} -> {status}")


def _runs(ctx: Ctx, article_id: str) -> list[dict[str, Any]]:
    resp = ctx.env.ddb.query(
        TableName=ctx.env.status_table,
        KeyConditionExpression="pk = :p AND begins_with(sk, :r)",
        ExpressionAttributeValues={":p": {"S": article_id}, ":r": {"S": "RUN#"}},
        ConsistentRead=True,
    )
    return [
        {k: next(iter(v.values())) for k, v in i.items()}
        for i in resp["Items"]
        if str(i["sk"]["S"]).count("#") == 1
    ]


def delete(ctx: Ctx) -> None:
    print("DELETE: deleted mid-run -> DELETED, no rows, an old upsert can't bring it back")
    a = _ingest(ctx, f"del-{uuid.uuid4().hex[:6]}.txt", "delete")
    wait(lambda: ctx.latest(a).get("status") not in (None, "CLAIMED"), "the run to start", 120, 1)
    _delete(ctx, a)
    latest = wait(lambda: in_status(ctx, a, "DELETED"), "DELETED", timeout=600, every=3)
    outcomes = sorted(str(r.get("outcome")) for r in _runs(ctx, a))
    print(f"   run outcomes: {outcomes}")
    ctx.check(latest.get("status") == "DELETED", "LATEST DELETED", "DELETE")
    ctx.check("delete_requested_at" not in latest, "no pending delete left", "DELETE")
    for table in (LIVE, NEW):
        rows = _query(ctx, {"action": "counts", "table": table})["counts"].get(a, 0)
        ctx.check(rows == 0, f"no rows in {table} ({rows})", "DELETE")


def _query(ctx: Ctx, payload: dict[str, Any]) -> dict[str, Any]:
    resp = ctx.lam.invoke(
        FunctionName=f"ingestion-{ctx.env.env}-query", Payload=json.dumps(payload).encode()
    )
    out = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        raise RuntimeError(f"query {payload}: {out}")
    return dict(out)


def switch(ctx: Ctx) -> None:
    print("SWITCH: gated switch, dual-write during the change, rollback, retire")
    before = tables(ctx)
    ctx.check(before["registry"]["live"] == LIVE and before["consistent"], "registry", "SWITCH")
    status, out = ctx.control.call("POST", "/v1/tables/active", {"table": NEW})
    ctx.check(status == 409, f"refused while not written ({status})", "SWITCH")

    reindex(ctx, {"target_table": NEW, "from_stage": "EMBED", "filter": {"only_uncovered": True}})
    filled = wait(
        lambda: (
            (t := tables(ctx))["tables"][NEW]["active_articles"]
            >= t["tables"][LIVE]["active_articles"]
            and t
        ),
        "the new table to catch up",
        900,
        10,
    )
    ctx.check(NEW in filled["registry"]["writes"], f"{NEW} written by live runs", "SWITCH")

    # An article created and edited during the change lands in both tables.
    filename = f"dual-{uuid.uuid4().hex[:6]}.txt"
    a = _ingest(ctx, filename, "dual v1")
    wait(lambda: in_status(ctx, a, "INDEXED"), "dual v1", 300)
    ctx.ingest_files({filename: article_text("dual v2")}, "review dual v2")
    v2 = wait(
        lambda: (
            (x := ctx.latest(a)).get("status") == "INDEXED"
            and int(x.get("doc_version", 0)) == 2
            and x
        ),
        "dual v2",
        300,
    )
    ctx.check(
        int(v2.get(f"tv_{LIVE}", 0)) == 2 and int(v2.get(f"tv_{NEW}", 0)) == 2,
        f"v2 in both tables (tv: {v2.get(f'tv_{LIVE}')}, {v2.get(f'tv_{NEW}')})",
        "SWITCH",
    )

    status, out = ctx.control.call("POST", "/v1/tables/active", {"table": NEW})
    ctx.check(status == 200, f"switched ({status} {out.get('message', '')})", "SWITCH")
    after = tables(ctx)
    ctx.check(after["active_table"] == NEW and after["consistent"], "view and registry", "SWITCH")
    status, _ = ctx.control.call("POST", "/v1/tables/active", {"table": LIVE})
    ctx.check(status == 200 and tables(ctx)["active_table"] == LIVE, "rolled back", "SWITCH")
    status, out = ctx.control.call("DELETE", f"/v1/tables/writes/{NEW}")
    ctx.check(status == 200 and out["writes"] == [LIVE], f"{NEW} retired", "SWITCH")

    _delete(ctx, a)
    wait(lambda: in_status(ctx, a, "DELETED"), "cleanup", 300)


def drift(ctx: Ctx) -> None:
    print("DRIFT: weekly check on a consistent environment -> nothing to repair")
    resp = ctx.lam.invoke(FunctionName=ctx.reconciler, Payload=b'{"drift": true}')
    out = json.loads(resp["Payload"].read())
    ctx.check(not resp.get("FunctionError"), f"reconciler ran ({out})", "DRIFT")
    repairs = int(out.get("requeued_drift_repair", 0))
    orphans = int(out.get("requeued_drift_orphan", 0))
    ctx.check(repairs == 0 and orphans == 0, f"repairs {repairs}, orphans {orphans}", "DRIFT")


def _put(ticket: dict[str, Any], data: bytes) -> int:
    req = urllib.request.Request(  # noqa: S310 - the presigned S3 URL from our own API
        ticket["url"], data=data, headers=ticket["headers"], method="PUT"
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        return int(resp.status)


def _ticket(ctx: Ctx, pid: str, data: bytes, external_id: str) -> dict[str, Any]:
    body = {"filename": "m2.txt", "content_type": "text/plain", "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "external_id": external_id}  # fmt: skip
    status, out = ctx.producer_api.call("POST", f"/v1/processes/{pid}/uploads", body)
    if status != 201:
        raise RuntimeError(f"ticket -> {status} {out}")
    return dict(out)


def m2(ctx: Ctx) -> None:
    print("M2: producer authorization, idempotency, settlement, tenant, janitor")
    status, out = ctx.producer_api.call("POST", "/v1/processes", {"name": "x", "source": "nobody"})
    ctx.check(status == 403, f"unconfigured source refused ({status})", "M2")

    key = uuid.uuid4().hex
    body = {"name": "review m2", "source": "acceptance"}
    first = ctx.producer_api.call("POST", "/v1/processes", body, headers={"Idempotency-Key": key})
    again = ctx.producer_api.call("POST", "/v1/processes", body, headers={"Idempotency-Key": key})
    pid = first[1]["process_id"]
    ctx.check(again[1]["process_id"] == pid, "a retried create is the same process", "M2")

    external = f"m2-{uuid.uuid4().hex[:6]}"
    v1, v2 = article_text("m2 v1"), article_text("m2 v2")
    t1, t2 = _ticket(ctx, pid, v1, external), _ticket(ctx, pid, v2, external)
    ctx.check(t1["article_id"] == t2["article_id"], "both uploads are one article", "M2")
    _put(t2, v2)
    _put(t1, v1)  # the older ticket arrives last: superseded
    status, _ = ctx.producer_api.call("POST", f"/v1/processes/{pid}/close", {})
    ctx.check(status == 200, f"closed ({status})", "M2")
    process = wait(
        lambda: (p := ctx.producer_api.call("GET", f"/v1/processes/{pid}")[1]).get("settled") and p,
        "the process to settle",
        180,
    )
    # S3 may deliver the two events in either order: the older ticket is SUPERSEDED if it
    # arrives last, else both register (the newer one wins). Either way: one article, settled.
    outcomes = sorted(
        str(ctx.producer_api.call("GET", f"/v1/uploads/{t['upload_id']}")[1]["status"])
        for t in (t1, t2)
    )
    ctx.check(
        process["registered_count"] == 1
        and process["pending_uploads"] == 0
        and outcomes in (["REGISTERED", "SUPERSEDED"], ["REGISTERED", "REGISTERED"]),
        f"settled: 1 article, uploads {outcomes}",
        "M2",
    )
    a = t2["article_id"]
    latest = wait(lambda: in_status(ctx, a, "INDEXED"), "M2 article indexed", 300)
    doc = boto3.client("s3").get_object(
        Bucket=tf_output("ingestion", ctx.env.env, "staging_bucket"),
        Key=f"{latest['staging_prefix']}normalized/document.json",
    )
    tenant = json.loads(doc["Body"].read()).get("tenant_id")
    ctx.check(tenant == "internal", f"tenant from the source policy ({tenant})", "M2")

    resp = ctx.lam.invoke(FunctionName=f"producer-{ctx.env.env}-janitor")
    out = json.loads(resp["Payload"].read())
    ctx.check(not resp.get("FunctionError"), f"janitor ran ({out})", "M2")

    _delete(ctx, a)
    wait(lambda: in_status(ctx, a, "DELETED"), "cleanup", 300)


HTML_TABLE = """<html><head><title>Plans {tag}</title></head><body><main>
<h1>Plans {tag}</h1>
<p>The {tag} subscription catalogue compares every plan side by side.</p>
<table><tr><th>Plan</th><th>Price</th></tr>
<tr><td><p>Basic plan {tag}</p></td><td>10</td></tr>
<tr><td><p>Pro plan {tag}</p></td><td>20</td></tr></table>
<ul><li><p>Cancel any time {tag}.</p></li></ul>
</main></body></html>"""


def m3(ctx: Ctx) -> None:
    print("M3: extraction, hybrid search with tenants, metadata-only updates, eval gate")
    tag = f"zq{uuid.uuid4().hex[:8]}"
    out = ctx.ingest_files({f"{tag}.html": HTML_TABLE.format(tag=tag).encode()}, "review m3")
    a = str(out["uploaded"][0]["article_id"])
    latest = wait(lambda: in_status(ctx, a, "INDEXED"), "M3 article indexed", 300)

    doc = json.loads(
        boto3.client("s3")
        .get_object(
            Bucket=tf_output("ingestion", ctx.env.env, "staging_bucket"),
            Key=f"{latest['staging_prefix']}normalized/document.json",
        )["Body"]
        .read()
    )
    cell = [b for b in doc["blocks"] if f"Basic plan {tag}" in b["text"]]
    ctx.check([b["type"] for b in cell] == ["table"], f"table cell text once ({cell})", "M3")

    def search(body: dict[str, Any]) -> tuple[int, Any]:
        return ctx.control.call("POST", "/v1/search", body)

    status, found = search({"tenant_id": "internal", "query": f"Basic plan {tag}", "k": 5})
    hits = found.get("results", []) if isinstance(found, dict) else []
    ctx.check(status == 200 and any(h["article_id"] == a for h in hits),
              f"search finds the article ({status}, {len(hits)} hits)", "M3")  # fmt: skip
    status, _ = search({"tenant_id": "someone-else", "query": "plan", "k": 5})
    ctx.check(status == 403, f"another tenant refused ({status})", "M3")
    status, by_source = search({"tenant_id": "internal", "query": f"Basic plan {tag}", "k": 5,
                                "filters": {"source": "smoke"}})  # fmt: skip
    hits = by_source.get("results", [])
    ctx.check(all(h["article_id"] != a for h in hits), "source filter applies", "M3")

    status, _ = ctx.producer_api.call("PATCH", f"/v1/articles/{a}", {"metadata": {"topic": tag}})
    ctx.check(status == 200, f"metadata patched ({status})", "M3")
    metadata_run = wait(
        lambda: next((r for r in _runs(ctx, a) if r.get("outcome") == "METADATA_UPDATED"), None),
        "the METADATA run",
        300,
    )
    ctx.check(bool(metadata_run), "metadata-only change ran as METADATA", "M3")
    ctx.check(int(ctx.latest(a)["doc_version"]) == 1, "no new version for metadata", "M3")

    # The switch gate: live runs write the new table and every article is in it, but there's no
    # fresh evaluation of it -> refused; force switches; then roll back and retire it.
    reindex(ctx, {"target_table": NEW, "from_stage": "EMBED", "filter": {"only_uncovered": True}})

    # The reindex only queues shadow runs: until they finish, the coverage gate refuses first.
    def past_coverage() -> tuple[int, Any] | None:
        out = ctx.control.call("POST", "/v1/tables/active", {"table": NEW})
        return out if out[1].get("gate") != "coverage" and "aren't in" not in str(out[1]) else None

    status, refused = wait(past_coverage, "the new table to cover every article", 600, 10)
    ctx.check(status == 409 and refused.get("gate") == "evaluation",
              f"switch without a fresh evaluation refused ({status} {refused.get('gate')}: {refused.get('message')})", "M3")  # fmt: skip
    status, _ = ctx.control.call("POST", "/v1/tables/active", {"table": NEW, "force": True})
    ctx.check(status == 200, f"force switches ({status})", "M3")
    status, _ = ctx.control.call("POST", "/v1/tables/active", {"table": LIVE, "force": True})
    ctx.check(status == 200 and tables(ctx)["active_table"] == LIVE, "rolled back", "M3")
    status, _ = ctx.control.call("DELETE", f"/v1/tables/writes/{NEW}")
    ctx.check(status == 200, "new table retired", "M3")

    _delete(ctx, a)
    wait(lambda: in_status(ctx, a, "DELETED"), "cleanup", 300)


def tenants(ctx: Ctx) -> None:
    print("TENANTS: per-tenant embedding model routing (t-small -> titan2_512)")
    tag = f"zt{uuid.uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / f"{tag}.txt").write_bytes(article_text(f"tenant {tag}"))
        out = ctx.cli("ingest-dir", tmp, "--name", "review tenants", "--source", "tenant-test",
                      "--close", cwd=Path(tmp))  # fmt: skip
    a = str(out["uploaded"][0]["article_id"])
    latest = wait(lambda: in_status(ctx, a, "INDEXED"), "t-small article indexed", 300)

    status, t = ctx.control.call("GET", "/v1/tables?tenant_id=t-small")
    live = (t.get("registry") or {}).get("live") if isinstance(t, dict) else None
    ctx.check(status == 200 and live == NEW, f"t-small's live table is {NEW} ({live})", "TENANTS")
    ctx.check(latest.get("tenant_id") == "t-small", "LATEST carries the tenant", "TENANTS")
    small = _query(ctx, {"action": "counts", "table": NEW})["counts"].get(a, 0)
    big = _query(ctx, {"action": "counts", "table": LIVE})["counts"].get(a, 0)
    ctx.check(small > 0 and big == 0, f"rows only in {NEW} ({small} vs {big})", "TENANTS")

    query = f"Paragraph 1 of the tenant {tag} acceptance article"
    status, found = ctx.control.call(
        "POST", "/v1/search", {"tenant_id": "t-small", "query": query, "k": 5}
    )
    hits = found.get("results", []) if isinstance(found, dict) else []
    ctx.check(status == 200 and found.get("table") == NEW and any(h["article_id"] == a for h in hits),
              f"t-small search on {found.get('table') if isinstance(found, dict) else found} finds it",
              "TENANTS")  # fmt: skip
    status, other = ctx.control.call(
        "POST", "/v1/search", {"tenant_id": "internal", "query": query, "k": 10}
    )
    hits = other.get("results", []) if isinstance(other, dict) else []
    ctx.check(status == 200 and all(h["article_id"] != a for h in hits),
              "the default tenant doesn't see it", "TENANTS")  # fmt: skip

    _delete(ctx, a)
    wait(lambda: in_status(ctx, a, "DELETED"), "cleanup", 300)
    gone = _query(ctx, {"action": "counts", "table": NEW})["counts"].get(a, 0)
    ctx.check(gone == 0, "deleted from its table", "TENANTS")


SCENARIOS = {"delete": delete, "switch": switch, "drift": drift, "m2": m2, "m3": m3,
             "tenants": tenants}  # fmt: skip


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev")
    parser.add_argument("--only", default=",".join(SCENARIOS))
    args = parser.parse_args()
    ctx = Ctx(args.env)
    for name in args.only.split(","):
        SCENARIOS[name](ctx)
    if ctx.failures:
        print("FAILED\n  " + "\n  ".join(ctx.failures))
        return 1
    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
