"""Embedding models on Amazon Bedrock (InvokeModel). The request format follows the model family:

    amazon.titan-embed-text-v2*  {"inputText", "dimensions", "normalize"}: one text per call,
                                 normalized by the model, inputTextTokenCount per text
    cohere.embed-*  (v3, v4)     {"texts": [<= 96], "input_type", "truncate": "END",
                                 "embedding_types": ["float"]} (+ "output_dimension" on v4):
                                 a batch per call, normalized here when the profile says so

Throttling and 5xx come back as botocore ClientErrors, which errors.classify maps (E_THROTTLED,
E_UPSTREAM_5XX); the client itself retries a few times first (aws._BEDROCK).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from ingest.embedding.base import Embedding, l2_normalize, spread_tokens
from ingest.embedding.profiles import COHERE_V4, TITAN_V2, EmbeddingProfile, bedrock_family

if TYPE_CHECKING:
    from mypy_boto3_bedrock_runtime.client import BedrockRuntimeClient

__all__ = ["BedrockCohereEmbedder", "BedrockTitanEmbedder", "Embedding", "bedrock_embedder"]

TOKEN_HEADER = "x-amzn-bedrock-input-token-count"  # noqa: S105 - a header name


def _invoke(client: BedrockRuntimeClient, model: str, body: dict[str, Any]) -> dict[str, Any]:
    resp = client.invoke_model(
        modelId=model,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    payload: dict[str, Any] = json.loads(resp["body"].read())
    headers = (resp.get("ResponseMetadata") or {}).get("HTTPHeaders") or {}
    payload.setdefault("_input_tokens", int(headers.get(TOKEN_HEADER) or 0))
    return payload


class BedrockTitanEmbedder:
    """Amazon Titan Text Embeddings v2: one text per call (the Embed step runs calls in a
    thread pool); normalization is the model's."""

    batch_size = 1

    def __init__(self, client: BedrockRuntimeClient, profile: EmbeddingProfile) -> None:
        self.client = client
        self.profile = profile

    def embed(self, text: str) -> Embedding:
        body = {
            "inputText": text,
            "dimensions": self.profile.dimension,
            "normalize": self.profile.normalize,
        }
        payload = _invoke(self.client, self.profile.model, body)
        return Embedding(
            vector=[float(x) for x in payload["embedding"]],
            tokens=int(payload.get("inputTextTokenCount", 0)),
        )

    def embed_documents(self, texts: Sequence[str]) -> list[Embedding]:
        return [self.embed(t) for t in texts]

    def embed_query(self, text: str) -> Embedding:
        return self.embed(text)


class BedrockCohereEmbedder:
    """Cohere Embed v3 (english, multilingual) and v4 on Bedrock: up to 96 texts per call, input
    type search_document / search_query, long inputs truncated at the end (Validate flags chunks
    over the profile's token limit, so truncation isn't silent)."""

    def __init__(self, client: BedrockRuntimeClient, profile: EmbeddingProfile) -> None:
        self.client = client
        self.profile = profile
        self.batch_size = profile.effective_batch_size

    def _embed(self, texts: Sequence[str], input_type: str | None) -> list[Embedding]:
        body: dict[str, Any] = {
            "texts": list(texts),
            "truncate": "END",
            "embedding_types": ["float"],
        }
        if input_type:
            body["input_type"] = input_type
        if self.profile.family == COHERE_V4:
            body["output_dimension"] = self.profile.dimension
        payload = _invoke(self.client, self.profile.model, body)
        raw = payload.get("embeddings")
        vectors = raw.get("float") if isinstance(raw, dict) else raw  # with/without types
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            got = len(vectors) if isinstance(vectors, list) else type(vectors).__name__
            raise ValueError(f"{self.profile.model} returned {got} embeddings for {len(texts)}")
        tokens = spread_tokens(int(payload.get("_input_tokens") or 0), texts)
        out = []
        for vector, n in zip(vectors, tokens, strict=True):
            values = [float(x) for x in vector]
            out.append(Embedding(l2_normalize(values) if self.profile.normalize else values, n))
        return out

    def embed_documents(self, texts: Sequence[str]) -> list[Embedding]:
        return self._embed(texts, self.profile.input_type("document"))

    def embed_query(self, text: str) -> Embedding:
        return self._embed([text], self.profile.input_type("query"))[0]


def bedrock_embedder(
    client: BedrockRuntimeClient, profile: EmbeddingProfile
) -> BedrockTitanEmbedder | BedrockCohereEmbedder:
    """The embedder for a Bedrock profile's model family (ProfileError for an unknown one)."""
    if bedrock_family(profile.model) == TITAN_V2:
        return BedrockTitanEmbedder(client, profile)
    return BedrockCohereEmbedder(client, profile)
