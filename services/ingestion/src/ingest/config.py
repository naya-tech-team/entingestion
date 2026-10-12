"""Runtime configuration from the Lambda environment. Each function needs only a subset."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

from ingest.embedding import profiles
from ingest.embedding.profiles import EmbeddingProfile


class MissingSetting(RuntimeError):
    pass


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _bool(name: str, *, default: bool) -> bool:
    return os.environ.get(name, str(default)).lower() == "true"


@dataclass(frozen=True)
class Settings:
    pipeline_version: str
    status_table: str
    cache_table: str
    staging_bucket: str
    producer_articles_table: str
    producer_process_table: str
    state_machine_arn: str
    # Embedding profiles (ingest.embedding.profiles): name -> model; the default registry's
    # first profile; tenant -> its first profile.
    embedding_profiles: Mapping[str, EmbeddingProfile]
    default_profile: str
    tenant_profiles: Mapping[str, str]
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_sslmode: str
    db_sslrootcert: str
    db_direct_host: str
    db_secret_arn: str
    lease_seconds: int
    event_bus_name: str
    alert_topic_arn: str
    tokenizer_encoding: str
    chunk_target_tokens: int
    chunk_overlap_tokens: int
    chunk_min_tokens: int
    max_chunk_tokens: int
    parent_child_enabled: bool
    parent_target_tokens: int
    child_target_tokens: int
    child_min_tokens: int
    embed_concurrency: int
    lambda_max_source_bytes: int
    run_ttl_days: int
    step_ttl_days: int
    cache_ttl_days: int
    ingest_queue_url: str
    ingest_dlq_url: str
    backfill_queue_url: str
    reindex_state_machine_arn: str
    tables_function_name: str
    allow_truncate: bool
    query_function_name: str
    reconcile_batch: int
    ocr_enabled: bool
    ocr_min_chars_per_page: int
    ocr_max_bytes: int
    source_access_parameter: str
    rerank_enabled: bool
    rerank_model_arn: str
    eval_tolerance: float
    eval_max_age_days: int
    pii_screening: bool
    pii_min_score: float
    live_slots: int
    backfill_slots: int
    backfill_queue_arn: str
    backfill_dlq_url: str
    slot_wait_seconds: int
    max_chunks_per_article: int
    ocr_max_polls: int

    @classmethod
    def from_env(cls) -> Settings:
        e = os.environ
        embedding = profiles.parse(e)
        return cls(
            pipeline_version=e.get("PIPELINE_VERSION", "v1"),
            status_table=e.get("STATUS_TABLE", ""),
            cache_table=e.get("CACHE_TABLE", ""),
            staging_bucket=e.get("STAGING_BUCKET", ""),
            producer_articles_table=e.get("PRODUCER_ARTICLES_TABLE", ""),
            producer_process_table=e.get("PRODUCER_PROCESS_TABLE", ""),
            state_machine_arn=e.get("STATE_MACHINE_ARN", ""),
            embedding_profiles=embedding.profiles,
            default_profile=embedding.default,
            tenant_profiles=embedding.tenants,
            db_host=e.get("DB_HOST", ""),
            db_port=_int("DB_PORT", 5432),
            db_name=e.get("DB_NAME", "ingest"),
            db_user=e.get("DB_USER", "ingest_writer"),
            db_sslmode=e.get("DB_SSLMODE", "verify-full"),
            # "system" for RDS Proxy (Amazon Trust Services certs); the bundled RDS CA bundle
            # when connecting straight to an RDS/Aurora endpoint.
            db_sslrootcert=e.get("DB_SSLROOTCERT", "system"),
            db_direct_host=e.get("DB_DIRECT_HOST", ""),
            db_secret_arn=e.get("DB_SECRET_ARN", ""),
            lease_seconds=_int("LEASE_SECONDS", 2100),
            event_bus_name=e.get("EVENT_BUS_NAME", "default"),
            alert_topic_arn=e.get("ALERT_TOPIC_ARN", ""),
            tokenizer_encoding=e.get("TOKENIZER_ENCODING", "cl100k_base"),
            chunk_target_tokens=_int("CHUNK_TARGET_TOKENS", 512),
            chunk_overlap_tokens=_int("CHUNK_OVERLAP_TOKENS", 64),
            chunk_min_tokens=_int("CHUNK_MIN_TOKENS", 64),
            max_chunk_tokens=_int("MAX_CHUNK_TOKENS", 2048),
            # Small children for matching, their section-level parent as context (chunker.py).
            parent_child_enabled=_bool("PARENT_CHILD_ENABLED", default=False),
            parent_target_tokens=_int("PARENT_TARGET_TOKENS", 1024),
            child_target_tokens=_int("CHILD_TARGET_TOKENS", 256),
            child_min_tokens=_int("CHILD_MIN_TOKENS", 128),
            embed_concurrency=_int("EMBED_CONCURRENCY", 4),
            lambda_max_source_bytes=_int("LAMBDA_MAX_SOURCE_BYTES", 50 * 1024 * 1024),
            run_ttl_days=_int("RUN_TTL_DAYS", 365),
            step_ttl_days=_int("STEP_TTL_DAYS", 90),
            cache_ttl_days=_int("CACHE_TTL_DAYS", 180),
            ingest_queue_url=e.get("INGEST_QUEUE_URL", ""),
            ingest_dlq_url=e.get("INGEST_DLQ_URL", ""),
            backfill_queue_url=e.get("BACKFILL_QUEUE_URL", ""),
            reindex_state_machine_arn=e.get("REINDEX_STATE_MACHINE_ARN", ""),
            tables_function_name=e.get("TABLES_FUNCTION_NAME", ""),
            # The migrate function's truncate (rebuild test); false in prod.
            allow_truncate=e.get("ALLOW_TRUNCATE", "true").lower() == "true",
            query_function_name=e.get("QUERY_FUNCTION_NAME", ""),
            reconcile_batch=_int("RECONCILE_BATCH", 500),
            # Textract OCR for scanned/large PDFs; off where Textract isn't available (free plan).
            ocr_enabled=e.get("OCR_ENABLED", "true").lower() == "true",
            # A PDF with less text than this per page is treated as scanned and sent to OCR.
            ocr_min_chars_per_page=_int("OCR_MIN_CHARS_PER_PAGE", 25),
            ocr_max_bytes=_int("OCR_MAX_BYTES", 500 * 1024 * 1024),  # Textract async limit
            # The producer's per-source access policy (SSM): search authorization, PII policy.
            source_access_parameter=e.get("SOURCE_ACCESS_PARAMETER", ""),
            # Bedrock reranking of the fused search results; off by default (paid).
            rerank_enabled=_bool("RERANK_ENABLED", default=False),
            rerank_model_arn=e.get("RERANK_MODEL_ARN", ""),
            # Switch gate: the target table's recall@10 may trail the live table's by this much.
            eval_tolerance=float(e.get("EVAL_TOLERANCE", "0.02")),
            eval_max_age_days=_int("EVAL_MAX_AGE_DAYS", 7),
            # Comprehend PII screening in Normalize; off by default (paid, not on the free plan).
            pii_screening=_bool("PII_SCREENING", default=False),
            pii_min_score=float(e.get("PII_MIN_SCORE", "0.8")),
            # Admission control (dispatcher): how many runs may hold a slot per lane at once
            # (0 = no limit). A record that finds every slot busy stays on its queue.
            live_slots=_int("LIVE_SLOTS", 8),
            backfill_slots=_int("BACKFILL_SLOTS", 2),
            backfill_queue_arn=e.get("BACKFILL_QUEUE_ARN", ""),
            backfill_dlq_url=e.get("BACKFILL_DLQ_URL", ""),
            # How long one dispatcher invocation waits for a free slot before handing the rest
            # of its batch back to the queue.
            slot_wait_seconds=_int("SLOT_WAIT_SECONDS", 40),
            # More chunks than this is E_TOO_LARGE (permanent): bounds Bedrock cost per article.
            max_chunks_per_article=_int("MAX_CHUNKS_PER_ARTICLE", 5000),
            # OcrPoll gives up (E_TIMEOUT, retryable) after this many status checks.
            ocr_max_polls=_int("OCR_MAX_POLLS", 180),
        )

    @property
    def default_target(self) -> EmbeddingProfile:
        return self.embedding_profiles[self.default_profile]

    @property
    def target_table(self) -> str:
        """The default profile's table: the default registry's table before it has an item."""
        return self.default_target.table

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise MissingSetting(f"missing settings: {', '.join(missing)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
