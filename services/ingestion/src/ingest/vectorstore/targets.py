"""Vector tables and the embedding profile each one holds: one table per model and dimension.
Every configured embedding profile (ingest.embedding.profiles) is a table, chunks_<profile>; the
migrate function creates them (ingest.vectorstore.profile_tables), reindex fills a new one, and
a switch makes one live.

Each table keeps its own staged vectors and manifest (embeddings/<table>.parquet and
_manifest.<table>.json), so re-embedding into a new table never overwrites what the current
table can be rebuilt from.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from ingest import ids
from ingest.config import get_settings
from ingest.embedding.profiles import EmbeddingProfile
from ingest.errors import ErrorCode, make_error


@dataclass(frozen=True)
class Target:
    profile: EmbeddingProfile

    @property
    def table(self) -> str:
        return self.profile.table

    @property
    def model_id(self) -> str:
        return self.profile.model

    @property
    def dimension(self) -> int:
        return self.profile.dimension

    @property
    def normalize(self) -> bool:
        return self.profile.normalize

    @property
    def vectors_file(self) -> str:
        return ids.vectors_file(self.table)

    @property
    def manifest_file(self) -> str:
        return ids.manifest_file(self.table)

    def cache_key(self, embed_text: str) -> str:
        """Embedding cache key: sha256(embed_text)#model@dim@norm#<profile fingerprint>, so
        different endpoints or settings never share entries."""
        key = ids.embedding_cache_key(embed_text, self.model_id, self.dimension, self.normalize)
        return f"{key}#{self.profile.fingerprint}"


class _Targets(Mapping[str, Target]):
    """table -> Target for every configured profile, read from the settings when used (tests
    and Lambdas see the environment's profiles)."""

    def _all(self) -> dict[str, Target]:
        profiles = get_settings().embedding.profiles
        return {p.table: Target(p) for p in profiles.values()}

    def __getitem__(self, table: str) -> Target:
        return self._all()[table]

    def __iter__(self) -> Iterator[str]:
        return iter(self._all())

    def __len__(self) -> int:
        return len(self._all())


TARGETS: Mapping[str, Target] = _Targets()


def target(table: str) -> Target:
    try:
        return TARGETS[table]
    except KeyError:
        raise make_error(ErrorCode.BUG, f"unknown target table {table!r}") from None


def profile_table(profile: str) -> str:
    return f"chunks_{profile}"
