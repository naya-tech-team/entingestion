"""What every embedding provider returns, and the interface the steps use."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Embedding:
    vector: list[float]
    tokens: int


class Embedder(Protocol):
    """One profile's model. embed_documents takes at most batch_size texts per call (the Embed
    step batches); embed_query embeds a search query (its input type may differ)."""

    batch_size: int

    def embed_documents(self, texts: Sequence[str]) -> list[Embedding]: ...

    def embed_query(self, text: str) -> Embedding: ...


def l2_normalize(vector: list[float]) -> list[float]:
    """Unit length (cosine = dot product). A zero vector stays as it is (Validate flags it)."""
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm > 0 else vector


def spread_tokens(total: int, texts: Sequence[str]) -> list[int]:
    """A batch's token total spread over its texts by length (batch APIs report one total)."""
    if total <= 0 or not texts:
        return [0] * len(texts)
    sizes = [max(1, len(t)) for t in texts]
    whole = sum(sizes)
    return [round(total * s / whole) for s in sizes]
