"""Search for the control API's POST /v1/search (outside the VPC, so it can reach Bedrock):

1. the tenant's registry names its live table (ingest.vectorstore.registry: the tenant's own, or
   the default one); embed the query with that table's embedding profile (as a query),
2. the query function (inside the VPC, ingest_reader, one tenant by row-level security) returns
   the fused vector + keyword candidates from that table (query.search_hybrid),
3. optionally Bedrock reranks them (amazon.rerank-v1:0, RERANK_ENABLED, off by default); on any
   rerank error the fused order stands.

Authorization (who may search which tenant) is the caller's job: control_api checks it against
the producer's source access policy (ingest.access) before calling search().
"""

from __future__ import annotations

import json
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from ingest import aws
from ingest.config import get_settings
from ingest.embedding.factory import make_embedder
from ingest.vectorstore import registry
from ingest.vectorstore.targets import target

logger = Logger()
metrics = Metrics()

RERANK_POOL = 2  # candidates per wanted result when reranking
MAX_CANDIDATES = 100  # the query function's search_hybrid limit
RERANK_TEXT_CHARS = 4000  # per candidate sent to the reranker


def call_query(payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke the query function (inside the VPC)."""
    resp = aws.lambda_client().invoke(
        FunctionName=get_settings().query_function_name, Payload=json.dumps(payload).encode()
    )
    out = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        raise RuntimeError(f"query {payload['action']} failed: {out.get('errorMessage', out)}")
    return dict(out)


def search(
    *, tenant_id: str, query: str, k: int, filters: dict[str, Any], rerank: bool
) -> dict[str, Any]:
    settings = get_settings()
    live = target(registry.load(aws.status_store(), tenant_id).live)
    vector = make_embedder(live.profile).embed_query(query).vector
    use_rerank = rerank and settings.rerank_enabled
    candidates = min(k * RERANK_POOL, MAX_CANDIDATES) if use_rerank else k
    hits: list[dict[str, Any]] = call_query(
        {
            "action": "search_hybrid",
            "tenant_id": tenant_id,
            "vector": vector,
            "text": query,
            "k": candidates,
            "filters": filters,
            "table": live.table,  # the tenant's live table (not chunks_active: per tenant)
        }
    )["results"]
    reranked = False
    if use_rerank and len(hits) > 1:
        try:
            hits = rerank_hits(query, hits, k)
            reranked = True
        except Exception:
            logger.warning("rerank failed; keeping the fused order", exc_info=True)
            metrics.add_metric(name="RerankFailed", unit=MetricUnit.Count, value=1)
    return {"table": live.table, "reranked": reranked, "results": hits[:k]}


def rerank_hits(query: str, hits: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    """Bedrock Rerank (bedrock-agent-runtime): hits in the model's order, with its score."""
    resp = aws.bedrock_agent().rerank(
        queries=[{"type": "TEXT", "textQuery": {"text": query}}],
        sources=[
            {
                "type": "INLINE",
                "inlineDocumentSource": {
                    "type": "TEXT",
                    "textDocument": {"text": str(h["text"])[:RERANK_TEXT_CHARS]},
                },
            }
            for h in hits
        ],
        rerankingConfiguration={
            "type": "BEDROCK_RERANKING_MODEL",
            "bedrockRerankingConfiguration": {
                "numberOfResults": min(k, len(hits)),
                "modelConfiguration": {"modelArn": get_settings().rerank_model_arn},
            },
        },
    )
    return [
        hits[int(r["index"])] | {"rerank_score": float(r["relevanceScore"])}
        for r in resp["results"]
    ]
