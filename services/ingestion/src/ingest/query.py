"""query Lambda: read-only pgvector access from inside the VPC, as ingest_reader (IAM auth).

Functions outside the VPC (no NAT, so no Bedrock or SQS from inside) call it with an action:

    {"action": "search_hybrid", "tenant_id": "t", "vector": [...], "text": "...", "k": 10,
     "filters": {"source": "news", "published_after": "2026-01-01", "process_id": "prc_..."},
     "table": null}
                                                        one tenant's best chunks: vector and
                                                        keyword candidates, reciprocal rank
                                                        fusion (the control API's /v1/search)
    {"action": "describe"}                              live table (chunks_active) and dimension
    {"action": "search", "vectors": [[...]], "k": 50, "table": null}
                                                        nearest active chunks per query vector
                                                        (evaluation)
    {"action": "counts", "table": null}                 active chunks per article
    {"action": "versions", "table": "t", "after": "", "limit": 500}
                                                        a page of (article, active version,
                                                        chunks) in article order (drift check)

"table" picks a registered vector table instead of the live view, e.g. to evaluate a reindexed
table before switching to it.

Row-level security (each vector table's policies, ingest.vectorstore.profile_tables) shows
ingest_reader one tenant at a time: search_hybrid sets
app.tenant_id for its transaction (and filters on it too). describe, search, counts and versions
are internal (evaluation, drift check) and read every tenant (app.all_tenants). Settings are
SET LOCAL: they end with the transaction, which every call rolls back.

HNSW search: ef_search = 2 x candidates within [SEARCH_MIN_EF_SEARCH, SEARCH_MAX_EF_SEARCH]
(100 and 1000 by default), with iterative scans (pgvector 0.8) so a filtered query still finds
enough rows; the default ef_search of 40 can't return k = 50. Fusion: score = sum(1 / (SEARCH_RRF_K
+ rank)) over the vector and keyword lists of at least SEARCH_MIN_POOL candidates each.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from psycopg import sql

from ingest.config import get_settings, require
from ingest.vectorstore.db import connection
from ingest.vectorstore.targets import TARGETS

logger = Logger()

REQUIRED = ("db.host",)
LIVE_VIEW = "chunks_active"
MAX_K = 200
MAX_HYBRID_K = 100


def _relation(table: str | None) -> sql.Composable:
    if table is None:
        return sql.Identifier(LIVE_VIEW)
    if table not in TARGETS:
        raise ValueError(f"unknown table {table!r}; known: {sorted(TARGETS)}")
    return sql.Identifier(table)


def _literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{float(x):.7g}" for x in vector) + "]"


def all_tenants(conn: Any) -> None:
    """For this transaction, see every tenant's rows. Only the internal actions do this; they
    never take a tenant from the caller."""
    conn.execute("SELECT set_config('app.all_tenants', 'on', true)")


def one_tenant(conn: Any, tenant_id: str) -> None:
    """For this transaction, see only `tenant_id`'s rows."""
    conn.execute(
        "SELECT set_config('app.tenant_id', %s, true), set_config('app.all_tenants', 'off', true)",
        (tenant_id,),
    )


def tune(conn: Any, candidates: int) -> None:
    """HNSW settings for this transaction: enough candidates for `candidates` results, and
    iterative scans so filters (tenant, source, dates) don't starve the result."""
    knobs = get_settings().search
    ef = min(max(knobs.min_ef_search, 2 * candidates), knobs.max_ef_search)
    conn.execute(
        "SELECT set_config('hnsw.ef_search', %s, true),"
        " set_config('hnsw.iterative_scan', 'relaxed_order', true)",
        (str(ef),),
    )


def describe(conn: Any) -> dict[str, Any]:
    row = conn.execute(
        "SELECT a.atttypmod FROM pg_attribute a"
        " WHERE a.attrelid = 'chunks_active'::regclass AND a.attname = 'embedding'"
    ).fetchone()
    dimension = int(row[0]) if row else None
    table = next(
        (
            t.table
            for t in TARGETS.values()
            if t.dimension == dimension
            and conn.execute(
                "SELECT position(%s in pg_get_viewdef('chunks_active'::regclass)) > 0", (t.table,)
            ).fetchone()[0]
        ),
        None,
    )
    return {"active_table": table, "dimension": dimension}


def search(conn: Any, vectors: list[list[float]], k: int, table: str | None) -> list[Any]:
    k = max(1, min(int(k), MAX_K))
    tune(conn, k)
    rel = _relation(table)
    where = sql.SQL("") if table is None else sql.SQL(" WHERE is_active")
    # With relaxed_order the index may return rows slightly out of order: sort them again.
    query = sql.SQL(
        "SELECT article_id, chunk_id, 1 - distance AS score FROM ("
        "SELECT article_id, chunk_id, embedding <=> %s::halfvec AS distance"
        " FROM {rel}{where} ORDER BY embedding <=> %s::halfvec LIMIT %s) nearest"
        " ORDER BY distance"
    ).format(rel=rel, where=where)
    results = []
    for vector in vectors:
        literal = _literal(vector)
        rows = conn.execute(query, (literal, literal, k)).fetchall()
        results.append([{"article_id": a, "chunk_id": c, "score": float(s)} for a, c, s in rows])
    return results


_HYBRID = """
WITH vec AS MATERIALIZED (
  SELECT chunk_id, row_number() OVER (ORDER BY distance, chunk_id) AS rank FROM (
    SELECT chunk_id, embedding <=> %(vector)s::halfvec AS distance FROM {rel}
    WHERE {where} ORDER BY embedding <=> %(vector)s::halfvec LIMIT %(pool)s) v
),
kw AS MATERIALIZED (
  SELECT chunk_id, row_number() OVER (ORDER BY score DESC, chunk_id) AS rank FROM (
    SELECT chunk_id, ts_rank_cd(content_tsv, q) AS score
    FROM {rel}, websearch_to_tsquery('english', %(text)s) q
    WHERE {where} AND content_tsv @@ q ORDER BY score DESC LIMIT %(pool)s) w
),
fused AS (
  SELECT chunk_id, sum(1.0 / (%(rrf)s + rank)) AS score,
         min(rank) FILTER (WHERE list = 'vector') AS vector_rank,
         min(rank) FILTER (WHERE list = 'keyword') AS keyword_rank
  FROM (SELECT chunk_id, rank, 'vector' AS list FROM vec
        UNION ALL SELECT chunk_id, rank, 'keyword' FROM kw) ranked
  GROUP BY chunk_id
)
SELECT c.chunk_id, c.article_id, c.doc_version, c.text, c.parent_text, c.heading_path,
       c.page_start, c.page_end, c.char_start, c.char_end, c.source, c.published_at,
       c.metadata->>'title', f.score, f.vector_rank, f.keyword_rank
FROM fused f JOIN {rel} c ON c.chunk_id = f.chunk_id
ORDER BY f.score DESC, c.chunk_id
LIMIT %(k)s
"""

_HIT_FIELDS = (
    "chunk_id",
    "article_id",
    "doc_version",
    "text",
    "context",
    "heading_path",
    "page_start",
    "page_end",
    "char_start",
    "char_end",
    "source",
    "published_at",
    "title",
    "score",
    "vector_rank",
    "keyword_rank",
)


def search_hybrid(
    conn: Any,
    *,
    tenant_id: str,
    vector: list[float],
    text: str,
    k: int,
    filters: dict[str, Any] | None = None,
    table: str | None = None,
) -> list[dict[str, Any]]:
    """One tenant's best k chunks: the nearest vectors and the best keyword matches (weighted
    tsvector, headings first), fused by reciprocal rank. "context" is the chunk's parent text
    (parent-child chunking), else its own text."""
    if not tenant_id:
        raise ValueError("search_hybrid needs a tenant_id")
    k = max(1, min(int(k), MAX_HYBRID_K))
    knobs = get_settings().search
    pool = max(2 * k, knobs.min_pool)
    one_tenant(conn, tenant_id)
    tune(conn, pool)
    filters = filters or {}
    conditions = [sql.SQL("tenant_id = %(tenant)s")]
    params: dict[str, Any] = {
        "tenant": tenant_id,
        "vector": _literal(vector),
        "text": text,
        "pool": pool,
        "rrf": knobs.rrf_k,
        "k": k,
    }
    if table is not None:
        conditions.append(sql.SQL("is_active"))
    if filters.get("source"):
        conditions.append(sql.SQL("source = %(source)s"))
        params["source"] = str(filters["source"])
    if filters.get("published_after"):
        conditions.append(sql.SQL("published_at >= %(published_after)s"))
        params["published_after"] = date.fromisoformat(str(filters["published_after"]))
    if filters.get("process_id"):
        conditions.append(sql.SQL("process_id = %(process_id)s"))
        params["process_id"] = str(filters["process_id"])
    query = sql.SQL(_HYBRID).format(rel=_relation(table), where=sql.SQL(" AND ").join(conditions))
    hits = []
    for row in conn.execute(query, params).fetchall():
        hit = dict(zip(_HIT_FIELDS, row, strict=True))
        hit["context"] = hit["context"] or hit["text"]
        hit["score"] = float(hit["score"])
        hit["heading_path"] = list(hit["heading_path"] or [])
        if hit["published_at"] is not None:
            hit["published_at"] = hit["published_at"].isoformat()
        hits.append(hit)
    return hits


def counts(conn: Any, table: str | None) -> dict[str, int]:
    rel = _relation(table)
    where = sql.SQL("") if table is None else sql.SQL(" WHERE is_active")
    rows = conn.execute(
        sql.SQL("SELECT article_id, count(*) FROM {rel}{where} GROUP BY article_id").format(
            rel=rel, where=where
        )
    ).fetchall()
    return {str(a): int(n) for a, n in rows}


MAX_PAGE = 5000


def versions(conn: Any, table: str, after: str, limit: int) -> dict[str, Any]:
    """A page of active (article_id, doc_version, chunk count) in article_id order, and the
    article_id to continue after (None at the end)."""
    limit = max(1, min(int(limit), MAX_PAGE))
    rows = conn.execute(
        sql.SQL(
            "SELECT article_id, doc_version, count(*) FROM {rel}"
            " WHERE is_active AND article_id > %s"
            " GROUP BY article_id, doc_version ORDER BY article_id LIMIT %s"
        ).format(rel=_relation(table)),
        (after, limit),
    ).fetchall()
    page = [[str(a), int(v), int(n)] for a, v, n in rows]
    return {"rows": page, "next": page[-1][0] if len(page) == limit else None}


_INTERNAL = ("describe", "search", "counts", "versions")


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    conn = connection(require(*REQUIRED))
    action = event.get("action")
    conn.rollback()  # a warm connection starts clean: no settings left from an earlier call
    try:
        if action == "search_hybrid":
            hits = search_hybrid(
                conn,
                tenant_id=str(event.get("tenant_id") or ""),
                vector=event["vector"],
                text=str(event.get("text") or ""),
                k=event.get("k", 10),
                filters=event.get("filters"),
                table=event.get("table"),
            )
            return {"results": hits}
        if action not in _INTERNAL:
            raise ValueError(f"unknown action {action!r}")
        all_tenants(conn)  # evaluation and the drift check cover every tenant
        if action == "describe":
            return describe(conn)
        if action == "search":
            return {
                "results": search(conn, event["vectors"], event.get("k", 50), event.get("table"))
            }
        if action == "counts":
            return {"counts": counts(conn, event.get("table"))}
        return versions(conn, event["table"], event.get("after") or "", event.get("limit", 500))
    finally:
        conn.rollback()  # read-only: never leave a transaction open on the cached connection
