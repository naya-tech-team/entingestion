"""embeddings/<table>.parquet: one row per chunk."""

from __future__ import annotations

import io
from dataclasses import dataclass

import pyarrow as pa
import pyarrow.parquet as pq


@dataclass(frozen=True)
class VectorRow:
    chunk_id: str
    embedding: list[float]
    model_tokens: int
    cache_hit: bool


_SCHEMA = pa.schema(
    [
        ("chunk_id", pa.string()),
        ("embedding", pa.list_(pa.float32())),
        ("model_tokens", pa.int32()),
        ("cache_hit", pa.bool_()),
    ]
)


def to_parquet(rows: list[VectorRow]) -> bytes:
    table = pa.Table.from_pydict(
        {
            "chunk_id": [r.chunk_id for r in rows],
            "embedding": [r.embedding for r in rows],
            "model_tokens": [r.model_tokens for r in rows],
            "cache_hit": [r.cache_hit for r in rows],
        },
        schema=_SCHEMA,
    )
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf.getvalue()


def from_parquet(data: bytes) -> list[VectorRow]:
    table = pq.read_table(io.BytesIO(data))
    cols = table.to_pydict()
    return [
        VectorRow(cid, [float(x) for x in emb], int(tok), bool(hit))
        for cid, emb, tok, hit in zip(
            cols["chunk_id"],
            cols["embedding"],
            cols["model_tokens"],
            cols["cache_hit"],
            strict=True,
        )
    ]
