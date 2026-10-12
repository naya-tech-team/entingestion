"""The embedder of a profile, with this container's clients."""

from __future__ import annotations

from ingest import aws
from ingest.embedding.base import Embedder
from ingest.embedding.bedrock import bedrock_embedder
from ingest.embedding.openai_compat import OpenAICompatibleEmbedder, api_key
from ingest.embedding.profiles import EmbeddingProfile


def _secret(name: str) -> str:
    return str(aws.secretsmanager().get_secret_value(SecretId=name)["SecretString"])


def make_embedder(profile: EmbeddingProfile) -> Embedder:
    if profile.provider == "bedrock":
        return bedrock_embedder(aws.bedrock(), profile)
    key = api_key(profile.api_key_secret, _secret) if profile.api_key_secret else None
    return OpenAICompatibleEmbedder(profile, key)
