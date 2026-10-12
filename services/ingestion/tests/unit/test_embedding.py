"""Embedding profiles (configuration), the providers (Bedrock Titan and Cohere request formats, an
OpenAI-compatible HTTP server stub), cache keys, batching in the Embed step and the Validate
checks that depend on the profile."""

from __future__ import annotations

import io
import json
import math
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import boto3
import pytest

from ingest import aws
from ingest.config import Settings, get_settings
from ingest.embedding import openai_compat
from ingest.embedding.base import Embedding, l2_normalize, spread_tokens
from ingest.embedding.bedrock import BedrockCohereEmbedder, BedrockTitanEmbedder, bedrock_embedder
from ingest.embedding.openai_compat import OpenAICompatibleEmbedder
from ingest.embedding.profiles import (
    EmbeddingProfile,
    ProfileError,
    builtin_profiles,
    parse,
)
from ingest.errors import ErrorCode, IngestPermanent, IngestThrottled, IngestTransient
from ingest.models.staging import Chunk
from ingest.vectorstore.targets import TARGETS, Target
from tests.conftest import REGION
from tests.unit.test_pipeline import (
    PAGE,
    FakeTitan,
    FakeVectorStore,
    latest,
    message_from,
    produce,
    run,
)

Env = tuple[FakeVectorStore, FakeTitan]

COHERE_V3 = {"provider": "bedrock", "model": "cohere.embed-multilingual-v3", "dimension": 1024}
LITELLM = {
    "provider": "openai_compatible",
    "model": "titan-v2",
    "dimension": 512,
    "base_url": "https://litellm.example.com/v1/",
    "api_key_secret": "entingestion/dev/litellm",
    "send_dimensions": True,
    "batch_size": 32,
}


@pytest.fixture(autouse=True)
def fresh() -> Iterator[None]:
    get_settings.cache_clear()
    openai_compat.reset_keys()
    yield
    get_settings.cache_clear()
    openai_compat.reset_keys()


# -- configuration ---------------------------------------------------------------------------


def test_without_configuration_the_original_tables_stay() -> None:
    config = parse({})
    assert config.default == "primary"
    assert {p.table for p in config.profiles.values()} == {
        "chunks_primary",
        "chunks_compact",
    }
    assert config.profiles["compact"].model == "amazon.titan-embed-text-v2:0"
    assert config.tenants == {}


def test_the_old_single_model_variables_still_pick_the_default() -> None:
    assert parse({"TARGET_TABLE": "chunks_compact"}).default == "compact"
    config = parse(
        {
            "TARGET_TABLE": "chunks_titan2_256",
            "EMBEDDING_MODEL_ID": "amazon.titan-embed-text-v2:0",
            "EMBEDDING_DIMENSION": "256",
        }
    )
    assert config.default == "titan2_256"
    assert config.profiles["titan2_256"].dimension == 256


def test_profiles_from_the_environment() -> None:
    config = parse(
        {
            "EMBEDDING_PROFILES": json.dumps(
                {"primary": {**COHERE_V3, "model": "amazon.titan-embed-text-v2:0"}}
                | {"multilingual": COHERE_V3, "litellm_512": LITELLM}
            ),
            "DEFAULT_PROFILE": "primary",
            "TENANT_PROFILES": json.dumps({"t-new": "multilingual"}),
        }
    )
    litellm = config.profiles["litellm_512"]
    assert (litellm.table, litellm.base_url, litellm.batch_size) == (
        "chunks_litellm_512",
        "https://litellm.example.com/v1",
        32,
    )
    assert config.profiles["multilingual"].input_type("document") == "search_document"
    assert config.profiles["multilingual"].input_type("query") == "search_query"
    assert config.tenants == {"t-new": "multilingual"}


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"EMBEDDING_PROFILES": "{}"}, "non-empty"),
        ({"EMBEDDING_PROFILES": json.dumps({"Bad-Name": COHERE_V3})}, "must match"),
        ({"EMBEDDING_PROFILES": json.dumps({"x": {**COHERE_V3, "dimension": 768}})}, "supports"),
        ({"EMBEDDING_PROFILES": json.dumps({"x": {**COHERE_V3, "model": "meta.x"}})}, "unsupp"),
        ({"EMBEDDING_PROFILES": json.dumps({"x": {**LITELLM, "base_url": None}})}, "base_url"),
        (
            {"EMBEDDING_PROFILES": json.dumps({"x": {**LITELLM, "base_url": "http://a.com"}})},
            "https",
        ),
        ({"EMBEDDING_PROFILES": json.dumps({"x": {**LITELLM, "dimension": 4001}})}, "dimension"),
        ({"DEFAULT_PROFILE": "nope"}, "isn't a profile"),
        ({"TENANT_PROFILES": json.dumps({"t": "nope"})}, "unknown profiles"),
    ],
)
def test_bad_profiles_are_refused(env: dict[str, str], message: str) -> None:
    with pytest.raises(ProfileError, match=message):
        parse(env)


def test_string_booleans_read_as_written() -> None:
    raw = {**COHERE_V3, "normalize": "false"}
    assert (
        not parse({"EMBEDDING_PROFILES": json.dumps({"x": raw}), "DEFAULT_PROFILE": "x"})
        .profiles["x"]
        .normalize
    )


def test_http_is_allowed_for_a_local_endpoint() -> None:
    config = parse(
        {
            "EMBEDDING_PROFILES": json.dumps(
                {"x": {**LITELLM, "base_url": "http://localhost:4000"}}
            ),
            "DEFAULT_PROFILE": "x",
        }
    )
    assert config.profiles["x"].base_url == "http://localhost:4000"


def test_settings_carry_the_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROFILES", json.dumps({"multilingual": COHERE_V3}))
    monkeypatch.setenv("DEFAULT_PROFILE", "multilingual")
    settings = Settings.from_env()
    assert settings.target_table == "chunks_multilingual"
    get_settings.cache_clear()
    assert list(TARGETS) == ["chunks_multilingual"]


# -- cache keys and staged file names --------------------------------------------------------


def test_titan_keeps_its_cache_keys_and_others_add_a_fingerprint() -> None:
    titan = Target(builtin_profiles()["primary"])
    assert titan.cache_key("hello").endswith("#amazon.titan-embed-text-v2:0@1024@norm")
    assert (titan.vectors_file, titan.manifest_file) == (
        "embeddings/vectors.parquet",
        "_manifest.json",
    )
    one = Target(EmbeddingProfile("a", "openai_compatible", "m", 512, base_url="https://x.io/v1"))
    other = Target(EmbeddingProfile("a", "openai_compatible", "m", 512, base_url="https://y.io"))
    assert one.cache_key("hello") != other.cache_key("hello")  # another endpoint: no sharing
    assert one.cache_key("hello").startswith(titan.cache_key("hello").split("#")[0])
    assert one.vectors_file == "embeddings/chunks_a.parquet"
    cohere = Target(EmbeddingProfile("c", "bedrock", "cohere.embed-english-v3", 1024))
    assert cohere.cache_key("hello").count("#") == 2


# -- Bedrock ---------------------------------------------------------------------------------


class FakeBedrock:
    def __init__(self, respond: Any, tokens: int | None = None) -> None:
        self.respond = respond
        self.tokens = tokens
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke_model(self, *, modelId: str, body: str, **_: Any) -> dict[str, Any]:
        request = json.loads(body)
        self.calls.append((modelId, request))
        headers = {} if self.tokens is None else {"x-amzn-bedrock-input-token-count": "12"}
        return {
            "body": io.BytesIO(json.dumps(self.respond(request)).encode()),
            "ResponseMetadata": {"HTTPHeaders": headers},
        }


def test_titan_sends_one_text_per_call() -> None:
    fake = FakeBedrock(lambda r: {"embedding": [0.6, 0.8], "inputTextTokenCount": 3})
    profile = builtin_profiles()["compact"]
    model = bedrock_embedder(fake, profile)  # type: ignore[arg-type]
    assert isinstance(model, BedrockTitanEmbedder)
    assert model.batch_size == 1
    out = model.embed_documents(["a b c", "d"])
    assert [e.tokens for e in out] == [3, 3]
    assert fake.calls[0] == (
        "amazon.titan-embed-text-v2:0",
        {"inputText": "a b c", "dimensions": 512, "normalize": True},
    )
    assert model.embed_query("q").vector == [0.6, 0.8]


@pytest.mark.parametrize("shape", ["typed", "plain"])
def test_cohere_batches_with_input_types_and_normalizes(shape: str) -> None:
    def respond(request: dict[str, Any]) -> dict[str, Any]:
        vectors = [[3.0, 4.0] for _ in request["texts"]]
        return {"embeddings": {"float": vectors} if shape == "typed" else vectors}

    fake = FakeBedrock(respond, tokens=12)
    profile = EmbeddingProfile(
        "m3", "bedrock", "cohere.embed-multilingual-v3", 1024, batch_size=200
    )
    model = bedrock_embedder(fake, profile)  # type: ignore[arg-type]
    assert isinstance(model, BedrockCohereEmbedder)
    assert model.batch_size == 96  # Cohere's limit per call

    out = model.embed_documents(["aaa", "bbbbbbbbb"])
    assert [e.vector for e in out] == [[0.6, 0.8], [0.6, 0.8]]  # unit length
    assert [e.tokens for e in out] == [3, 9]  # the call's total, spread by length
    _, request = fake.calls[0]
    assert request == {
        "texts": ["aaa", "bbbbbbbbb"],
        "input_type": "search_document",
        "truncate": "END",
        "embedding_types": ["float"],
    }
    model.embed_query("who")
    assert fake.calls[1][1]["input_type"] == "search_query"


def test_cohere_v4_asks_for_its_dimension() -> None:
    fake = FakeBedrock(lambda r: {"embeddings": {"float": [[1.0, 0.0]]}})
    profile = EmbeddingProfile("v4", "bedrock", "cohere.embed-v4:0", 512, normalize=False)
    bedrock_embedder(fake, profile).embed_query("q")  # type: ignore[arg-type]
    assert fake.calls[0][1]["output_dimension"] == 512


def test_cohere_with_the_wrong_number_of_embeddings_fails() -> None:
    fake = FakeBedrock(lambda r: {"embeddings": [[1.0]]})
    profile = EmbeddingProfile("m3", "bedrock", "cohere.embed-english-v3", 1024)
    with pytest.raises(ValueError, match="1 embeddings for 2"):
        bedrock_embedder(fake, profile).embed_documents(["a", "b"])  # type: ignore[arg-type]


def test_an_unknown_bedrock_family_is_refused() -> None:
    profile = EmbeddingProfile("x", "bedrock", "amazon.titan-embed-image-v1", 1024)
    with pytest.raises(ProfileError, match="unsupported Bedrock"):
        bedrock_embedder(FakeBedrock(dict), profile)  # type: ignore[arg-type]


# -- OpenAI-compatible HTTP (a stub server) --------------------------------------------------


class Stub:
    """A tiny OpenAI embeddings server: records requests, answers with `reply`."""

    def __init__(self) -> None:
        self.requests: list[tuple[dict[str, str], dict[str, Any]]] = []
        self.statuses: list[int] = []  # served first, one per request
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append((dict(self.headers), body))
                status = stub.statuses.pop(0) if stub.statuses else 200
                if status == 200:
                    data = [
                        {"index": i, "embedding": [3.0, 4.0 + i]} for i in range(len(body["input"]))
                    ][::-1]  # out of order: the client sorts by index
                    payload = {"data": data, "usage": {"prompt_tokens": 10}}
                else:
                    payload = {"error": {"message": f"status {status}"}}
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub() -> Iterator[Stub]:
    server = Stub()
    yield server
    server.close()


def http_profile(stub: Stub, **overrides: Any) -> EmbeddingProfile:
    fields: dict[str, Any] = {
        "name": "lite",
        "provider": "openai_compatible",
        "model": "bedrock-titan",
        "dimension": 2,
        "base_url": stub.url,
        "send_dimensions": True,
        "document_input_type": "search_document",
        "query_input_type": "search_query",
        "batch_size": 8,
    } | overrides
    return EmbeddingProfile(**fields)


def test_openai_compatible_posts_a_batch(stub: Stub) -> None:
    model = OpenAICompatibleEmbedder(http_profile(stub), "sk-test", sleep=lambda s: None)
    assert model.batch_size == 8

    out = model.embed_documents(["aa", "aaaaaaaa"])

    headers, body = stub.requests[0]
    assert headers["Authorization"] == "Bearer sk-test"
    assert body == {
        "model": "bedrock-titan",
        "input": ["aa", "aaaaaaaa"],
        "dimensions": 2,
        "input_type": "search_document",
    }
    assert out[0].vector == pytest.approx([0.6, 0.8])  # index 0, normalized
    assert math.isclose(math.hypot(*out[1].vector), 1.0)
    assert [e.tokens for e in out] == [2, 8]
    model.embed_query("q")
    assert stub.requests[1][1]["input_type"] == "search_query"


def test_openai_compatible_without_optional_fields(stub: Stub) -> None:
    profile = http_profile(stub, send_dimensions=False, document_input_type=None, normalize=False)
    out = OpenAICompatibleEmbedder(profile).embed_documents(["x"])
    headers, body = stub.requests[0]
    assert "Authorization" not in headers
    assert body == {"model": "bedrock-titan", "input": ["x"]}
    assert out[0].vector == [3.0, 4.0]  # not normalized


@pytest.mark.parametrize(
    ("statuses", "error", "code"),
    [
        ([429, 429, 429], IngestThrottled, ErrorCode.THROTTLED),
        ([503, 500, 502], IngestTransient, ErrorCode.UPSTREAM_5XX),
        ([400], IngestPermanent, ErrorCode.UPSTREAM_4XX),
        ([401], IngestPermanent, ErrorCode.UPSTREAM_4XX),
    ],
)
def test_openai_compatible_errors(
    stub: Stub, statuses: list[int], error: type[Exception], code: str
) -> None:
    stub.statuses = list(statuses)
    model = OpenAICompatibleEmbedder(http_profile(stub), "k", sleep=lambda s: None)
    with pytest.raises(error) as raised:
        model.embed_documents(["x"])
    assert raised.value.code == code  # type: ignore[attr-defined]
    assert len(stub.requests) == len(statuses)  # retried 429/5xx up to 3 times, 4xx never


def test_openai_compatible_recovers_after_a_throttle(stub: Stub) -> None:
    stub.statuses = [429, 200]
    waits: list[float] = []
    model = OpenAICompatibleEmbedder(http_profile(stub), "k", sleep=waits.append)
    assert len(model.embed_documents(["x"])) == 1
    assert len(waits) == 1


def test_an_unreachable_endpoint_is_transient() -> None:
    profile = EmbeddingProfile("x", "openai_compatible", "m", 2, base_url="http://127.0.0.1:9/v1")
    with pytest.raises(IngestTransient):
        OpenAICompatibleEmbedder(profile, timeout=2, sleep=lambda s: None).embed_query("x")


def test_the_api_key_comes_from_secrets_manager_once(aws: None) -> None:
    from ingest.embedding.factory import make_embedder

    sm = boto3.client("secretsmanager", region_name=REGION)
    sm.create_secret(Name="litellm-key", SecretString=json.dumps({"api_key": "sk-1"}))
    profile = EmbeddingProfile(
        "lite",
        "openai_compatible",
        "m",
        2,
        base_url="https://x.io",
        api_key_secret="litellm-key",  # noqa: S106
    )
    model = make_embedder(profile)
    assert isinstance(model, OpenAICompatibleEmbedder)
    assert model.key == "sk-1"
    sm.put_secret_value(SecretId="litellm-key", SecretString="sk-2")
    assert make_embedder(profile).key == "sk-1"  # cached per container
    openai_compat.reset_keys()
    assert make_embedder(profile).key == "sk-2"  # a plain-string secret works too


# -- helpers ---------------------------------------------------------------------------------


def test_normalize_and_token_spread() -> None:
    assert l2_normalize([3.0, 4.0]) == [0.6, 0.8]
    assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]
    assert spread_tokens(0, ["a", "b"]) == [0, 0]
    assert spread_tokens(10, ["a", "aaaa"]) == [2, 8]


# -- the Embed step batches; Validate follows the profile ------------------------------------


def test_embed_sends_batches_of_the_profile_size(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    _, titan = env
    titan.batch_size = 3
    sizes: list[int] = []
    real = titan.embed_documents

    def counted(texts: Any) -> list[Embedding]:
        sizes.append(len(texts))
        return real(texts)

    monkeypatch.setattr(titan, "embed_documents", counted)
    _, out = run(message_from(produce("art_b", PAGE)))

    assert out["action"] == "DONE"
    chunks = int(latest("art_b")["chunk_count"])
    assert sum(sizes) == chunks
    assert max(sizes) == 3
    assert len(sizes) == math.ceil(chunks / 3)


def chunk(cid: str, tokens: int) -> Chunk:
    return Chunk(
        chunk_id=cid,
        article_id="a",
        doc_version=1,
        ordinal=0,
        text="t",
        embed_text="t",
        token_count=tokens,
    )


def checks(**kwargs: Any) -> dict[str, bool]:
    from ingest.embedding.vectors import VectorRow
    from ingest.steps.validate import run_checks  # after the environment (Powertools metrics)

    vectors = kwargs.pop("vectors", [VectorRow("c1", [3.0, 4.0], 5, False)])
    out = run_checks(
        chunks=kwargs.pop("chunks", [chunk("c1", 10)]),
        vectors=vectors,
        article_id="a",
        doc_version=1,
        dimension=2,
        max_chunk_tokens=2048,
        **kwargs,
    )
    return {name: c.passed for name, c in out.items()}


def test_validate_skips_the_unit_length_check_without_normalization() -> None:
    assert checks()["normalized"] is False
    assert checks(normalized=False)["normalized"] is True


def test_validate_flags_chunks_over_the_models_input_limit() -> None:
    assert checks(max_input_tokens=512)["oversized"] is True
    assert checks(chunks=[chunk("c1", 600)], max_input_tokens=512)["oversized"] is False
    from ingest.embedding.vectors import VectorRow

    vectors = [VectorRow("c1", [0.6, 0.8], 900, False)]
    # A batch API's token count is an estimate: only exact per-input counts are checked.
    assert checks(vectors=vectors, max_input_tokens=512)["oversized"] is False
    assert (
        checks(vectors=vectors, max_input_tokens=512, model_tokens_exact=False)["oversized"] is True
    )


def test_profile_input_limits_are_capped_by_the_model_family() -> None:
    cohere = EmbeddingProfile("c", "bedrock", "cohere.embed-english-v3", 1024)
    assert cohere.input_token_limit == 512
    assert builtin_profiles()["primary"].input_token_limit == 8192
    assert builtin_profiles()["primary"].per_input_tokens
    assert not cohere.per_input_tokens


def test_a_pipeline_run_into_a_cohere_table(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Cohere default profile: its own table and staged files; chunks pass its 512-token
    limit (the test page's chunks are small)."""
    store, _ = env
    monkeypatch.setenv(
        "EMBEDDING_PROFILES", json.dumps({"multilingual": COHERE_V3, "primary": {
            "provider": "bedrock", "model": "amazon.titan-embed-text-v2:0", "dimension": 1024}})
    )  # fmt: skip
    monkeypatch.setenv("DEFAULT_PROFILE", "multilingual")
    aws.reset()

    _, out = run(message_from(produce("art_c", PAGE)))

    assert out["action"] == "DONE"
    tables: dict[str, FakeVectorStore] = store.tables  # type: ignore[attr-defined]
    assert {r["article_id"] for r in tables["chunks_multilingual"].rows.values()} == {"art_c"}
    assert latest("art_c")["tv_chunks_multilingual"] == 1
