"""Hybrid search (query function) against a real pgvector database, as
ingest_reader: one tenant per call, vector + keyword candidates fused by reciprocal rank."""

from __future__ import annotations

import math
from typing import Any

import pytest

from ingest.ids import chunk_id
from ingest.models.staging import Chunk
from ingest.query import search, search_hybrid
from ingest.vectorstore.index import ArticleAttributes, IndexRow, PgVectorStore
from tests.integration.test_tenants import as_role

pytestmark = pytest.mark.integration

TABLE = "chunks_primary"
DIM = 1024


def direction(*weights: tuple[int, float]) -> list[float]:
    """A unit vector with the given (axis, weight) components."""
    v = [0.0] * DIM
    for axis, w in weights:
        v[axis] = w
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


def write(
    pg: Any,
    article_id: str,
    chunks: list[tuple[list[str], str, list[float]]],
    *,
    tenant: str = "t-a",
    source: str = "news",
    published: str = "2026-03-01T00:00:00Z",
    process_id: str = "prc_1",
    parent_text: str | None = None,
) -> list[str]:
    out = []
    for ordinal, (heading, text, vector) in enumerate(chunks):
        chunk = Chunk(
            chunk_id=chunk_id(article_id, 1, ordinal),
            article_id=article_id,
            doc_version=1,
            ordinal=ordinal,
            parent_id="p" if parent_text else None,
            parent_text=parent_text,
            text=text,
            embed_text=text,
            heading_path=heading,
            char_start=ordinal * 100,
            char_end=ordinal * 100 + len(text),
            token_count=len(text.split()),
            metadata={"title": f"Title of {article_id}"},
        )
        attrs = ArticleAttributes(tenant, source, published, {})
        out.append(IndexRow(chunk, vector, process_id, attrs, "m", "v1"))
    PgVectorStore(pg, TABLE).write_and_swap(article_id, 1, out)
    return [r.chunk.chunk_id for r in out]


def test_hybrid_search_fuses_vector_and_keyword_ranks_within_one_tenant(pg: Any) -> None:
    [pricing_heading] = write(
        pg, "art_1", [(["Pricing"], "Plans start small and grow with you.", direction((0, 1)))]
    )
    [pricing_text] = write(
        pg, "art_2", [(["Intro"], "Our pricing is simple to follow.", direction((1, 1)))]
    )
    [near] = write(pg, "art_3", [(["Other"], "Unrelated words here.", direction((2, 1)))])
    write(pg, "art_x", [(["Pricing"], "pricing pricing pricing", direction((2, 1)))], tenant="t-b")
    query = direction((2, 1), (0, 0.9))  # nearest: art_3, then art_1

    with as_role(pg, "ingest_reader") as conn:
        hits = search_hybrid(conn, tenant_id="t-a", vector=query, text="pricing", k=10)
        assert conn.execute("SHOW hnsw.ef_search").fetchone()[0] == "100"
        assert conn.execute("SHOW hnsw.iterative_scan").fetchone()[0] == "relaxed_order"

    ids = [h["chunk_id"] for h in hits]
    assert set(ids) == {pricing_heading, pricing_text, near}  # never another tenant's rows
    # art_1 is second by vector and first by keyword (its heading is weighted A): first overall.
    assert ids[0] == pricing_heading
    top = hits[0]
    assert (top["vector_rank"], top["keyword_rank"]) == (2, 1)
    assert top["article_id"] == "art_1"
    assert top["heading_path"] == ["Pricing"]
    assert top["title"] == "Title of art_1"
    assert (top["char_start"], top["char_end"]) == (0, len("Plans start small and grow with you."))
    assert top["context"] == top["text"]  # no parent: the chunk itself
    assert top["score"] == pytest.approx(1 / 62 + 1 / 61)
    by_id = {h["chunk_id"]: h for h in hits}
    assert by_id[pricing_text]["keyword_rank"] == 2  # body-only match ranks below the heading


def test_hybrid_search_filters_and_returns_the_parent_as_context(pg: Any) -> None:
    v = direction((0, 1))
    write(pg, "art_old", [([], "alpha old", v)], published="2025-01-01T00:00:00Z")
    write(pg, "art_wire", [([], "alpha wire", v)], source="wire")
    write(pg, "art_p2", [([], "alpha other process", v)], process_id="prc_2")
    [child] = write(
        pg, "art_child", [([], "alpha child", v)], parent_text="The whole section: alpha child."
    )

    with as_role(pg, "ingest_reader") as conn:
        news = search_hybrid(conn, tenant_id="t-a", vector=v, text="alpha", k=10,
                             filters={"source": "news"})  # fmt: skip
        recent = search_hybrid(conn, tenant_id="t-a", vector=v, text="alpha", k=10,
                               filters={"published_after": "2026-01-01"})  # fmt: skip
        prc = search_hybrid(conn, tenant_id="t-a", vector=v, text="alpha", k=10,
                            filters={"process_id": "prc_2"})  # fmt: skip
        nobody = search_hybrid(conn, tenant_id="t-zzz", vector=v, text="alpha", k=10)

    assert {h["article_id"] for h in news} == {"art_old", "art_p2", "art_child"}
    assert {h["article_id"] for h in recent} == {"art_wire", "art_p2", "art_child"}
    assert [h["article_id"] for h in prc] == ["art_p2"]
    assert nobody == []
    child_hit = next(h for h in news if h["chunk_id"] == child)
    assert child_hit["context"] == "The whole section: alpha child."
    assert child_hit["published_at"] == "2026-03-01"


def test_a_tenant_is_required(pg: Any) -> None:
    with as_role(pg, "ingest_reader") as conn, pytest.raises(ValueError, match="tenant"):
        search_hybrid(conn, tenant_id="", vector=direction((0, 1)), text="x", k=5)


def test_vector_search_returns_k_50_through_the_hnsw_index(pg: Any) -> None:
    """With pgvector's default ef_search (40) an HNSW scan returns at most 40 rows; the query
    function raises it for k = 50 (evaluation's request)."""
    for i in range(120):
        write(pg, f"art_{i:03d}", [([], f"chunk {i}", direction((0, 1), (1 + i % 7, 0.1 * i)))])
    literal = "[" + ",".join(map(str, direction((0, 1)))) + "]"
    nearest = "SELECT chunk_id FROM chunks_active ORDER BY embedding <=> %s::halfvec LIMIT 50"
    with as_role(pg, "ingest_reader") as conn:
        # A small table: make the planner take the HNSW index, as it does at size.
        for setting in ("enable_seqscan", "enable_bitmapscan", "enable_sort"):
            conn.execute(f"SET LOCAL {setting} = off")
        conn.execute("SELECT set_config('app.all_tenants', 'on', true)")
        plan = " ".join(str(r[0]) for r in conn.execute(f"EXPLAIN {nearest}", (literal,)))
        assert "embedding_hnsw" in plan  # through RLS and the security_invoker view
        assert len(conn.execute(nearest, (literal,)).fetchall()) == 40  # the default
        [results] = search(conn, [direction((0, 1))], 50, None)
    assert len(results) == 50
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_headings_weigh_more_than_text(pg: Any) -> None:
    write(pg, "art_w", [(["Refund policy"], "Money comes back.", direction((0, 1)))])
    tsv = pg.execute(f"SELECT content_tsv::text FROM {TABLE} WHERE article_id = 'art_w'")  # noqa: S608
    text = tsv.fetchone()[0]
    assert "'refund':1A" in text
    assert "'money':3B" in text or "'money':3" in text
