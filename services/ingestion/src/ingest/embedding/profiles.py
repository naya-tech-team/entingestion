"""Embedding profiles: which model embeds into which vector table (docs/ARCHITECTURE.md
"Embedding models").

A profile is a name plus how to call its model. Its table is chunks_<name>, so the default
profiles reproduce the tables that existed before profiles:

    titan2_1024   bedrock  amazon.titan-embed-text-v2:0  1024  -> chunks_titan2_1024
    titan2_512    bedrock  amazon.titan-embed-text-v2:0   512  -> chunks_titan2_512

Configured by Terraform (variable embedding_profiles) as JSON in the environment:

    EMBEDDING_PROFILES  {"<name>": {"provider": "bedrock" | "openai_compatible", "model": ...,
                         "dimension": ..., "normalize": true, "base_url": ...,
                         "api_key_secret": ..., "document_input_type": ...,
                         "query_input_type": ..., "send_dimensions": false, "batch_size": 16,
                         "max_input_tokens": 8192}, ...}
    DEFAULT_PROFILE     the default registry's table when it has no item yet
    TENANT_PROFILES     {"<tenant>": "<profile>"}: a tenant's first table
                        (ingest.vectorstore.registry)

Without EMBEDDING_PROFILES (functions deployed before profiles) the two default profiles apply,
and TARGET_TABLE / EMBEDDING_MODEL_ID / EMBEDDING_DIMENSION still pick the default one.

Providers:
    bedrock            InvokeModel; the request format follows the model family (model id prefix):
                       amazon.titan-embed-text-v2 (one text per call) or cohere.embed-* (batches)
    openai_compatible  POST {base_url}/embeddings, the OpenAI embeddings API (LiteLLM, vLLM, TEI,
                       Ollama, ...), over plain HTTPS; API key from Secrets Manager
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

Provider = Literal["bedrock", "openai_compatible"]
PROVIDERS: tuple[Provider, ...] = ("bedrock", "openai_compatible")

NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
MAX_DIMENSION = 4000  # pgvector's HNSW limit for halfvec
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

TITAN_V2 = "amazon.titan-embed-text-v2"
COHERE = "cohere.embed-"
COHERE_V4 = "cohere.embed-v4"
COHERE_MAX_BATCH = 96  # texts per InvokeModel

# What each Bedrock family accepts (dimensions) and its input limit in tokens. A profile's
# max_input_tokens is capped at the family's limit.
TITAN_DIMENSIONS = frozenset({256, 512, 1024})
COHERE_V3_DIMENSIONS = frozenset({1024})
COHERE_V4_DIMENSIONS = frozenset({256, 512, 1024, 1536})
_FAMILY_MAX_TOKENS = {TITAN_V2: 8192, COHERE_V4: 128_000, COHERE: 512}

DEFAULT_PROFILE = "titan2_1024"
TITAN_MODEL = "amazon.titan-embed-text-v2:0"


class ProfileError(ValueError):
    """An embedding profile that can't be used as configured."""


def bedrock_family(model: str) -> str:
    """The Bedrock request format of a model id: TITAN_V2, COHERE_V4 or COHERE (v3)."""
    if model.startswith(TITAN_V2):
        return TITAN_V2
    if model.startswith(COHERE_V4):
        return COHERE_V4
    if model.startswith(COHERE):
        return COHERE
    raise ProfileError(
        f"unsupported Bedrock embedding model {model!r}: expected {TITAN_V2}* or {COHERE}*"
    )


@dataclass(frozen=True)
class EmbeddingProfile:
    name: str
    provider: Provider
    model: str
    dimension: int
    normalize: bool = True  # unit-length vectors (cosine = dot product; Validate checks it)
    base_url: str | None = None
    api_key_secret: str | None = None  # Secrets Manager name (openai_compatible)
    document_input_type: str | None = None
    query_input_type: str | None = None
    send_dimensions: bool = False  # openai_compatible: send "dimensions" (Matryoshka models)
    batch_size: int = 16
    max_input_tokens: int = 8192

    @property
    def table(self) -> str:
        return f"chunks_{self.name}"

    @property
    def family(self) -> str:
        """bedrock: the model family (request format); openai_compatible: the provider."""
        return bedrock_family(self.model) if self.provider == "bedrock" else self.provider

    @property
    def is_titan(self) -> bool:
        return self.provider == "bedrock" and self.family == TITAN_V2

    @property
    def per_input_tokens(self) -> bool:
        """The model reports each input's token count (Titan). Batch APIs report a total at
        most, which is spread over the batch: an estimate, not checked by Validate."""
        return self.is_titan

    @property
    def effective_batch_size(self) -> int:
        if self.is_titan:
            return 1  # one inputText per InvokeModel
        if self.provider == "bedrock":
            return max(1, min(self.batch_size, COHERE_MAX_BATCH))
        return max(1, self.batch_size)

    @property
    def input_token_limit(self) -> int:
        """The most tokens one input may have: the profile's limit, capped by the family's."""
        if self.provider == "bedrock":
            return min(self.max_input_tokens, _FAMILY_MAX_TOKENS[self.family])
        return self.max_input_tokens

    def input_type(self, purpose: Literal["document", "query"]) -> str | None:
        """input_type sent with a request (Cohere search_document / search_query by default)."""
        configured = self.document_input_type if purpose == "document" else self.query_input_type
        if configured or self.provider != "bedrock" or self.family == TITAN_V2:
            return configured
        return "search_document" if purpose == "document" else "search_query"

    @property
    def fingerprint(self) -> str:
        """What makes two profiles' vectors differ: provider, model, dimension, normalization,
        endpoint host and document input type. Part of the embedding cache key, so different
        endpoints or settings never share cache entries."""
        host = urlsplit(self.base_url).netloc if self.base_url else ""
        parts = (
            self.provider,
            self.model,
            str(self.dimension),
            str(self.normalize),
            host,
            self.input_type("document") or "",
            str(self.send_dimensions),
        )
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]

    def check(self) -> None:
        """Raise ProfileError if the profile can't work."""
        if not NAME.match(self.name):
            raise ProfileError(f"profile name {self.name!r} must match {NAME.pattern}")
        if self.provider not in PROVIDERS:
            raise ProfileError(f"{self.name}: provider must be one of {PROVIDERS}")
        if not self.model:
            raise ProfileError(f"{self.name}: model is required")
        if not 1 <= self.dimension <= MAX_DIMENSION:
            raise ProfileError(f"{self.name}: dimension must be 1..{MAX_DIMENSION}")
        if self.batch_size < 1 or self.max_input_tokens < 1:
            raise ProfileError(f"{self.name}: batch_size and max_input_tokens must be >= 1")
        if self.provider == "bedrock":
            self._check_bedrock()
        else:
            self._check_http()

    def _check_bedrock(self) -> None:
        allowed = {
            TITAN_V2: TITAN_DIMENSIONS,
            COHERE_V4: COHERE_V4_DIMENSIONS,
            COHERE: COHERE_V3_DIMENSIONS,
        }[self.family]
        if self.dimension not in allowed:
            raise ProfileError(
                f"{self.name}: {self.model} supports dimensions {sorted(allowed)}, "
                f"not {self.dimension}"
            )

    def _check_http(self) -> None:
        if not self.base_url:
            raise ProfileError(f"{self.name}: openai_compatible needs base_url")
        url = urlsplit(self.base_url)
        local = url.hostname in LOCAL_HOSTS
        if url.scheme != "https" and not (url.scheme == "http" and local):
            raise ProfileError(f"{self.name}: base_url must be https (http only for localhost)")


def _flag(value: Any) -> bool:
    """A JSON boolean (or its string form: "false" must not read as true)."""
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def _profile(name: str, raw: Mapping[str, Any]) -> EmbeddingProfile:
    def opt(key: str) -> str | None:
        value = raw.get(key)
        return str(value) if value not in (None, "") else None

    try:
        profile = EmbeddingProfile(
            name=name,
            provider=str(raw.get("provider") or "bedrock"),  # type: ignore[arg-type]  # checked
            model=str(raw.get("model") or ""),
            dimension=int(raw["dimension"]),
            normalize=_flag(raw.get("normalize", True)),
            base_url=(opt("base_url") or "").rstrip("/") or None,
            api_key_secret=opt("api_key_secret"),
            document_input_type=opt("document_input_type"),
            query_input_type=opt("query_input_type"),
            send_dimensions=_flag(raw.get("send_dimensions", False)),
            batch_size=int(raw.get("batch_size") or 16),
            max_input_tokens=int(raw.get("max_input_tokens") or 8192),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise ProfileError(f"profile {name!r}: {err}") from err
    profile.check()
    return profile


def builtin_profiles() -> dict[str, EmbeddingProfile]:
    """The profiles before configuration existed (and Terraform's default)."""
    return {
        p.name: p
        for p in (
            EmbeddingProfile(DEFAULT_PROFILE, "bedrock", TITAN_MODEL, 1024),
            # Same model, 512 dimensions: smaller and cheaper to search (migration 002).
            EmbeddingProfile("titan2_512", "bedrock", TITAN_MODEL, 512),
        )
    }


@dataclass(frozen=True)
class ProfileConfig:
    profiles: Mapping[str, EmbeddingProfile]
    default: str
    tenants: Mapping[str, str]  # tenant -> profile name


def parse(env: Mapping[str, str]) -> ProfileConfig:
    """Profiles, the default profile and the tenant mapping from the environment."""
    raw = env.get("EMBEDDING_PROFILES", "").strip()
    if raw:
        data = json.loads(raw)
        if not isinstance(data, dict) or not data:
            raise ProfileError("EMBEDDING_PROFILES must be a non-empty JSON object")
        profiles = {name: _profile(name, cfg) for name, cfg in data.items()}
    else:
        profiles = builtin_profiles()
    default = env.get("DEFAULT_PROFILE", "").strip() or _legacy_default(env, profiles)
    if default not in profiles:
        raise ProfileError(f"DEFAULT_PROFILE {default!r} isn't a profile: {sorted(profiles)}")
    tenants = json.loads(env.get("TENANT_PROFILES", "").strip() or "{}")
    if not isinstance(tenants, dict):
        raise ProfileError("TENANT_PROFILES must be a JSON object")
    unknown = sorted({str(p) for p in tenants.values()} - set(profiles))
    if unknown:
        raise ProfileError(f"TENANT_PROFILES names unknown profiles: {unknown}")
    return ProfileConfig(profiles, default, {str(t): str(p) for t, p in tenants.items()})


def _legacy_default(env: Mapping[str, str], profiles: dict[str, EmbeddingProfile]) -> str:
    """The default profile of an environment from before profiles: TARGET_TABLE names it; a
    table with no profile becomes one from EMBEDDING_MODEL_ID / EMBEDDING_DIMENSION."""
    table = env.get("TARGET_TABLE", "").strip()
    if not table:
        return DEFAULT_PROFILE
    name = table.removeprefix("chunks_")
    if name not in profiles:
        profiles[name] = _profile(
            name,
            {
                "provider": "bedrock",
                "model": env.get("EMBEDDING_MODEL_ID") or TITAN_MODEL,
                "dimension": env.get("EMBEDDING_DIMENSION") or 1024,
            },
        )
    return name
