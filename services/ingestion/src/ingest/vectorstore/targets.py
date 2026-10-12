"""Vector tables and the embedding profile each one holds (brief §7.3: one table per
model/dimension). Every configured embedding profile (ingest.embedding.profiles) is a table,
chunks_<profile>; the migrate function creates the tables of new profiles
(ingest.vectorstore.profile_tables), reindex fills them, and a switch makes one live.

Each table keeps its own staged vectors and manifest, so re-embedding into a new table never
overwrites what the current table can be rebuilt from. chunks_titan2_1024, the original table,
keeps the original file names, so data staged before tables existed stays usable.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from ingest.config import get_settings
from ingest.embedding.profiles import EmbeddingProfile
from ingest.errors import ErrorCode, make_error
from ingest.ids import STAGING_FILES, embedding_cache_key

# The first table: its staged files have no table in their names.
DEFAULT_TABLE = "chunks_titan2_1024"


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
    def is_default(self) -> bool:
        """The original table (original staged file names)."""
        return self.table == DEFAULT_TABLE

    @property
    def vectors_file(self) -> str:
        return STAGING_FILES["EMBED"] if self.is_default else f"embeddings/{self.table}.parquet"

    @property
    def manifest_file(self) -> str:
        return STAGING_FILES["VALIDATE"] if self.is_default else f"_manifest.{self.table}.json"

    def cache_key(self, embed_text: str) -> str:
        """Embedding cache key: sha256(embed_text)#model@dim@norm for Bedrock Titan (the key from
        before profiles, so its entries stay valid), plus #<profile fingerprint> for every other
        model, so different endpoints or settings never share entries."""
        key = embedding_cache_key(embed_text, self.model_id, self.dimension, self.normalize)
        return key if self.profile.is_titan else f"{key}#{self.profile.fingerprint}"


class _Targets(Mapping[str, Target]):
    """table -> Target for every configured profile, read from the settings when used (tests
    and Lambdas see the environment's profiles)."""

    def _all(self) -> dict[str, Target]:
        profiles = get_settings().embedding_profiles
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
