"""Runtime configuration from the Lambda environment, grouped by concern. Every environment read
of the service goes through here.

Each function needs only a subset: a handler names what it can't run without
(require(...)) and fails at its first invocation when one is missing. Tunables have code
defaults; Terraform sets the deployment-specific values (names, ARNs, URLs, profiles).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

from ingest.embedding import profiles
from ingest.embedding.profiles import EmbeddingProfile


class MissingSetting(RuntimeError):
    """A setting a function needs isn't configured, or isn't valid."""


_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


def _str(env: Mapping[str, str], name: str, default: str = "") -> str:
    return env.get(name, default).strip()


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        raise MissingSetting(f"{name} must be an integer, not {raw!r}") from None


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        raise MissingSetting(f"{name} must be a number, not {raw!r}") from None


def _bool(env: Mapping[str, str], name: str, *, default: bool) -> bool:
    """true/false (also 1/0, yes/no, on/off, any case); unset or empty is the default."""
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise MissingSetting(f"{name} must be true or false, not {raw!r}")


@dataclass(frozen=True)
class EmbeddingSettings:
    """Embedding profiles (ingest.embedding.profiles): name -> model; the default registry's
    first profile; tenant -> its first profile. concurrency: model calls in flight per Embed
    (and per evaluation)."""

    profiles: Mapping[str, EmbeddingProfile]
    default: str
    tenants: Mapping[str, str]
    concurrency: int

    @classmethod
    def from_env(cls, e: Mapping[str, str]) -> EmbeddingSettings:
        config = profiles.parse(e)
        return cls(
            profiles=config.profiles,
            default=config.default,
            tenants=config.tenants,
            concurrency=_int(e, "EMBED_CONCURRENCY", 4),
        )


@dataclass(frozen=True)
class DbSettings:
    host: str
    port: int
    name: str
    user: str
    sslmode: str
    # "system" for RDS Proxy (Amazon Trust Services certs); the bundled RDS CA bundle when
    # connecting straight to an RDS/Aurora endpoint.
    sslrootcert: str
    statement_timeout_ms: int
    # IAM token checks on a small burstable instance can take several seconds, mostly when cold.
    connect_timeout_seconds: int
    # migrate only: the writer endpoint, the master secret, the migrations and the CA bundle
    # (empty: the copies bundled in the Lambda zip, or the repository checkout's).
    direct_host: str
    secret_arn: str
    migrations_dir: str
    ca_bundle: str

    @classmethod
    def from_env(cls, e: Mapping[str, str]) -> DbSettings:
        return cls(
            host=_str(e, "DB_HOST"),
            port=_int(e, "DB_PORT", 5432),
            name=_str(e, "DB_NAME", "ingest"),
            user=_str(e, "DB_USER", "ingest_writer"),
            sslmode=_str(e, "DB_SSLMODE", "verify-full"),
            sslrootcert=_str(e, "DB_SSLROOTCERT", "system"),
            statement_timeout_ms=_int(e, "DB_STATEMENT_TIMEOUT_MS", 30_000),
            connect_timeout_seconds=_int(e, "DB_CONNECT_TIMEOUT_SECONDS", 30),
            direct_host=_str(e, "DB_DIRECT_HOST"),
            secret_arn=_str(e, "DB_SECRET_ARN"),
            migrations_dir=_str(e, "MIGRATIONS_DIR"),
            ca_bundle=_str(e, "DB_CA_BUNDLE"),
        )


@dataclass(frozen=True)
class QueueSettings:
    ingest_url: str
    ingest_dlq_url: str
    backfill_url: str
    backfill_arn: str
    backfill_dlq_url: str

    @classmethod
    def from_env(cls, e: Mapping[str, str]) -> QueueSettings:
        return cls(
            ingest_url=_str(e, "INGEST_QUEUE_URL"),
            ingest_dlq_url=_str(e, "INGEST_DLQ_URL"),
            backfill_url=_str(e, "BACKFILL_QUEUE_URL"),
            backfill_arn=_str(e, "BACKFILL_QUEUE_ARN"),
            backfill_dlq_url=_str(e, "BACKFILL_DLQ_URL"),
        )


@dataclass(frozen=True)
class ChunkingSettings:
    tokenizer_encoding: str
    target_tokens: int
    overlap_tokens: int
    min_tokens: int
    max_chunk_tokens: int
    # Small children for matching, their section-level parent as context (chunker.py).
    parent_child: bool
    parent_target_tokens: int
    child_target_tokens: int
    child_min_tokens: int
    # More chunks than this is E_TOO_LARGE (permanent): bounds the embedding cost per article.
    max_chunks_per_article: int

    @classmethod
    def from_env(cls, e: Mapping[str, str]) -> ChunkingSettings:
        return cls(
            tokenizer_encoding=_str(e, "TOKENIZER_ENCODING", "cl100k_base"),
            target_tokens=_int(e, "CHUNK_TARGET_TOKENS", 512),
            overlap_tokens=_int(e, "CHUNK_OVERLAP_TOKENS", 64),
            min_tokens=_int(e, "CHUNK_MIN_TOKENS", 64),
            max_chunk_tokens=_int(e, "MAX_CHUNK_TOKENS", 2048),
            parent_child=_bool(e, "PARENT_CHILD_ENABLED", default=False),
            parent_target_tokens=_int(e, "PARENT_TARGET_TOKENS", 1024),
            child_target_tokens=_int(e, "CHILD_TARGET_TOKENS", 256),
            child_min_tokens=_int(e, "CHILD_MIN_TOKENS", 128),
            max_chunks_per_article=_int(e, "MAX_CHUNKS_PER_ARTICLE", 5000),
        )


@dataclass(frozen=True)
class OcrSettings:
    """Textract OCR for scanned or large PDFs; off unless enabled (Textract isn't on every plan).
    A PDF with less text than min_chars_per_page per page is treated as scanned. OcrPoll gives
    up (E_TIMEOUT, retryable) after max_polls status checks."""

    enabled: bool
    min_chars_per_page: int
    max_bytes: int
    max_polls: int

    @classmethod
    def from_env(cls, e: Mapping[str, str]) -> OcrSettings:
        return cls(
            enabled=_bool(e, "OCR_ENABLED", default=False),
            min_chars_per_page=_int(e, "OCR_MIN_CHARS_PER_PAGE", 25),
            max_bytes=_int(e, "OCR_MAX_BYTES", 500 * 1024 * 1024),  # Textract's async limit
            max_polls=_int(e, "OCR_MAX_POLLS", 180),
        )


@dataclass(frozen=True)
class SearchSettings:
    """Hybrid search (ingest.query, ingest.search). Reciprocal rank fusion scores a chunk
    sum(1 / (rrf_k + rank)) over the vector and keyword lists, each min_pool candidates or more.
    HNSW ef_search is twice the candidates, within [min_ef_search, max_ef_search] (pgvector's
    limit is 1000). Bedrock reranking (paid, off by default) asks the query function for
    rerank_pool candidates per wanted result, at most max_candidates, and sends each one's
    first rerank_text_chars characters."""

    rrf_k: int
    min_pool: int
    min_ef_search: int
    max_ef_search: int
    rerank_enabled: bool
    rerank_model_arn: str
    rerank_pool: int
    max_candidates: int
    rerank_text_chars: int

    @classmethod
    def from_env(cls, e: Mapping[str, str]) -> SearchSettings:
        return cls(
            rrf_k=_int(e, "SEARCH_RRF_K", 60),
            min_pool=_int(e, "SEARCH_MIN_POOL", 50),
            min_ef_search=_int(e, "SEARCH_MIN_EF_SEARCH", 100),
            max_ef_search=_int(e, "SEARCH_MAX_EF_SEARCH", 1000),
            rerank_enabled=_bool(e, "RERANK_ENABLED", default=False),
            rerank_model_arn=_str(e, "RERANK_MODEL_ARN"),
            rerank_pool=_int(e, "SEARCH_RERANK_POOL", 2),
            max_candidates=_int(e, "SEARCH_MAX_CANDIDATES", 100),
            rerank_text_chars=_int(e, "SEARCH_RERANK_TEXT_CHARS", 4000),
        )


@dataclass(frozen=True)
class AccessSettings:
    """The producer's per-source access policy (SSM parameter): search authorization and each
    source's PII policy (ingest.access). A warm Lambda re-reads it after cache_seconds."""

    parameter: str
    cache_seconds: int

    @classmethod
    def from_env(cls, e: Mapping[str, str]) -> AccessSettings:
        return cls(
            parameter=_str(e, "SOURCE_ACCESS_PARAMETER"),
            cache_seconds=_int(e, "ACCESS_CACHE_SECONDS", 300),
        )


@dataclass(frozen=True)
class PiiSettings:
    """Comprehend PII screening in Normalize; off by default (paid, not on every plan)."""

    screening: bool
    min_score: float

    @classmethod
    def from_env(cls, e: Mapping[str, str]) -> PiiSettings:
        return cls(
            screening=_bool(e, "PII_SCREENING", default=False),
            min_score=_float(e, "PII_MIN_SCORE", 0.8),
        )


@dataclass(frozen=True)
class EvaluationSettings:
    """The switch gate: the target table's newest evaluation (at most max_age_days old) may
    trail the live table's recall@10 by at most tolerance."""

    tolerance: float
    max_age_days: int

    @classmethod
    def from_env(cls, e: Mapping[str, str]) -> EvaluationSettings:
        return cls(
            tolerance=_float(e, "EVAL_TOLERANCE", 0.02),
            max_age_days=_int(e, "EVAL_MAX_AGE_DAYS", 7),
        )


@dataclass(frozen=True)
class AdmissionSettings:
    """Admission control (ingest.dispatcher): how many runs may hold a slot per lane at once
    (0 = no limit). A record that finds every slot busy waits up to slot_wait_seconds, checking
    every slot_poll_seconds, then goes back to its queue, visible again after
    busy_visibility_seconds."""

    live_slots: int
    backfill_slots: int
    slot_wait_seconds: int
    slot_poll_seconds: float
    busy_visibility_seconds: int

    @classmethod
    def from_env(cls, e: Mapping[str, str]) -> AdmissionSettings:
        return cls(
            live_slots=_int(e, "LIVE_SLOTS", 8),
            backfill_slots=_int(e, "BACKFILL_SLOTS", 2),
            slot_wait_seconds=_int(e, "SLOT_WAIT_SECONDS", 40),
            slot_poll_seconds=_float(e, "DISPATCH_SLOT_POLL_SECONDS", 2.0),
            busy_visibility_seconds=_int(e, "DISPATCH_BUSY_VISIBILITY_SECONDS", 30),
        )


@dataclass(frozen=True)
class RetrySettings:
    """MarkFailed's retry budgets (ingest.steps.mark_failed). A transient failure is retried
    after base_seconds * 2^n, until max_retryable runs failed; throttling has its own budget,
    max_throttled runs, retried after throttle_seconds * 2^min(n, 3)."""

    max_retryable: int
    max_throttled: int
    base_seconds: int
    throttle_seconds: int

    @classmethod
    def from_env(cls, e: Mapping[str, str]) -> RetrySettings:
        return cls(
            max_retryable=_int(e, "RETRY_MAX_RETRYABLE", 5),
            max_throttled=_int(e, "RETRY_MAX_THROTTLED", 20),
            base_seconds=_int(e, "RETRY_BASE_SECONDS", 900),
            throttle_seconds=_int(e, "RETRY_THROTTLE_SECONDS", 300),
        )


@dataclass(frozen=True)
class ReconcileSettings:
    """The reconciler: at most batch items per sweep; a FAILED_RETRYABLE article with
    max_retries retries is escalated to FAILED_PERMANENT; STAGED for staged_grace_seconds is
    re-indexed; a producer update younger than stream_grace_seconds may still be on its way
    through the Pipe."""

    batch: int
    max_retries: int
    staged_grace_seconds: int
    stream_grace_seconds: int

    @classmethod
    def from_env(cls, e: Mapping[str, str]) -> ReconcileSettings:
        return cls(
            batch=_int(e, "RECONCILE_BATCH", 500),
            max_retries=_int(e, "RECONCILE_MAX_RETRIES", 5),
            staged_grace_seconds=_int(e, "RECONCILE_STAGED_GRACE_SECONDS", 30 * 60),
            stream_grace_seconds=_int(e, "RECONCILE_STREAM_GRACE_SECONDS", 5 * 60),
        )


@dataclass(frozen=True)
class RetentionSettings:
    """DynamoDB TTLs: RUN# items, step items and embedding-cache entries."""

    run_ttl_days: int
    step_ttl_days: int
    cache_ttl_days: int

    @classmethod
    def from_env(cls, e: Mapping[str, str]) -> RetentionSettings:
        return cls(
            run_ttl_days=_int(e, "RUN_TTL_DAYS", 365),
            step_ttl_days=_int(e, "STEP_TTL_DAYS", 90),
            cache_ttl_days=_int(e, "CACHE_TTL_DAYS", 180),
        )


@dataclass(frozen=True)
class Settings:
    pipeline_version: str
    status_table: str
    cache_table: str
    staging_bucket: str
    producer_articles_table: str
    producer_process_table: str
    state_machine_arn: str
    reindex_state_machine_arn: str
    tables_function_name: str
    query_function_name: str
    event_bus_name: str
    alert_topic_arn: str
    lease_seconds: int
    lambda_max_source_bytes: int
    # The migrate function's truncate (the rebuild test); only where explicitly allowed.
    allow_truncate: bool
    embedding: EmbeddingSettings
    db: DbSettings
    queues: QueueSettings
    chunking: ChunkingSettings
    ocr: OcrSettings
    search: SearchSettings
    access: AccessSettings
    pii: PiiSettings
    evaluation: EvaluationSettings
    admission: AdmissionSettings
    retry: RetrySettings
    reconcile: ReconcileSettings
    retention: RetentionSettings

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        return cls(
            pipeline_version=_str(e, "PIPELINE_VERSION", "v1"),
            status_table=_str(e, "STATUS_TABLE"),
            cache_table=_str(e, "CACHE_TABLE"),
            staging_bucket=_str(e, "STAGING_BUCKET"),
            producer_articles_table=_str(e, "PRODUCER_ARTICLES_TABLE"),
            producer_process_table=_str(e, "PRODUCER_PROCESS_TABLE"),
            state_machine_arn=_str(e, "STATE_MACHINE_ARN"),
            reindex_state_machine_arn=_str(e, "REINDEX_STATE_MACHINE_ARN"),
            tables_function_name=_str(e, "TABLES_FUNCTION_NAME"),
            query_function_name=_str(e, "QUERY_FUNCTION_NAME"),
            event_bus_name=_str(e, "EVENT_BUS_NAME", "default"),
            alert_topic_arn=_str(e, "ALERT_TOPIC_ARN"),
            lease_seconds=_int(e, "LEASE_SECONDS", 2100),
            lambda_max_source_bytes=_int(e, "LAMBDA_MAX_SOURCE_BYTES", 50 * 1024 * 1024),
            allow_truncate=_bool(e, "ALLOW_TRUNCATE", default=False),
            embedding=EmbeddingSettings.from_env(e),
            db=DbSettings.from_env(e),
            queues=QueueSettings.from_env(e),
            chunking=ChunkingSettings.from_env(e),
            ocr=OcrSettings.from_env(e),
            search=SearchSettings.from_env(e),
            access=AccessSettings.from_env(e),
            pii=PiiSettings.from_env(e),
            evaluation=EvaluationSettings.from_env(e),
            admission=AdmissionSettings.from_env(e),
            retry=RetrySettings.from_env(e),
            reconcile=ReconcileSettings.from_env(e),
            retention=RetentionSettings.from_env(e),
        )

    @property
    def default_target(self) -> EmbeddingProfile:
        return self.embedding.profiles[self.embedding.default]

    @property
    def target_table(self) -> str:
        """The default profile's table: the default registry's table before it has an item."""
        return self.default_target.table

    def value(self, name: str) -> object:
        """A setting by its dotted name, e.g. "db.host"."""
        found: object = self
        for part in name.split("."):
            found = getattr(found, part)
        return found

    def require(self, *names: str) -> None:
        """Raise MissingSetting naming every empty one of `names` (dotted, e.g. "db.host")."""
        missing = [n for n in names if not self.value(n)]
        if missing:
            raise MissingSetting(f"missing settings: {', '.join(missing)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def require(*names: str) -> Settings:
    """The settings, after checking that `names` are set (a handler's first lines)."""
    settings = get_settings()
    settings.require(*names)
    return settings
