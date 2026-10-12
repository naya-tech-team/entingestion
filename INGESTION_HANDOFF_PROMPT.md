# Handoff: Article Ingestion Pipeline (Terraform, reusable modules, Aurora pgvector)

You are building the ingestion pipeline for an article platform on AWS. It reads the **producer's** DynamoDB `articles` stream and turns every verified file into validated, searchable chunks in **Aurora PostgreSQL with pgvector**, tracking status per article, run and step.

Read the whole brief before writing code. Build in the phases in §15, one PR per phase, and stop at each gate for review.

---

## 1. Context: the producer (already built; don't modify it)

The producer (Python Lambda + API Gateway, Terraform) guarantees:

- A file is uploaded and **verified in S3 (size + SHA-256) before** its `articles` row is written.
- Raw keys are immutable: `raw/{article_id}/{upload_id}/{filename}`, bucket versioned and KMS-encrypted.
- `articles` (PK `article_id`, **Streams NEW_AND_OLD_IMAGES**):
  `process_id, first_process_id, source, external_id?, title?, filename, content_type, s3_uri, s3_key, s3_version_id?, content_sha256 (hex), size_bytes, upload_id, status ("STAGED"), metadata, created_at, updated_at, updated_date (YYYY-MM-DD)`.
  - GSI `gsi_updated`: `updated_date` / `updated_at`, projecting `content_sha256, process_id, upload_id`.
  - GSI `gsi_process`: `process_id` / `updated_at`.
- Every registration writes the row, even when the content is unchanged, so a MODIFY event arrives whenever an article joins a new process. Metadata-only edits (PATCH) also produce MODIFY with an unchanged `content_sha256`. Deletes produce REMOVE.
- `process` (PK `process_id`): `status (OPEN|CLOSED), expected_count, registered_count, rejected_count`. "Settled" = `CLOSED` and `registered_count + rejected_count >= expected_count`. Once settled, **`registered_count` is the number of articles this pipeline should expect for the process.**

Producer resources are looked up by name with Terraform data sources (§8.4); the pipeline never writes to them.

## 2. Requirements

1. **Effectively-once** ingestion on at-least-once delivery: no duplicate runs or chunks.
2. **Per-article, per-run, per-step status** in DynamoDB, queryable without scans.
3. **Every step stages its output in S3.** Any step can be re-run alone, and pgvector can be rebuilt from staging without re-extracting or re-embedding.
4. **Only validated data reaches pgvector** (a gate before indexing).
5. **Readers never see a half-updated article**: version N stays searchable until N+1 is complete.
6. **Transient errors retry automatically; permanent errors stop, alert, and can be replayed** after a fix.
7. **Manual triggers**: ingest, replay from a step, reindex/backfill, DLQ redrive.
8. **A reconciler guarantees completeness** (DynamoDB Streams keep only 24 h).
9. **All infrastructure in Terraform, built from reusable modules** (§8). No CDK, no console changes.

## 3. Tech stack

| Concern | Choice |
|---|---|
| Language | Python 3.12, `uv`, Pydantic v2, type hints, `ruff`, `mypy --strict` |
| Lambda | `python3.12`, `arm64`, AWS Lambda Powertools v3 (Logger, Metrics, Tracer, batch, event handler) |
| Orchestration | Step Functions **Standard**, one execution per article version; a separate Distributed Map state machine for reindex/backfill |
| Triggers | DynamoDB Streams → **EventBridge Pipes** → SQS ingest queue; EventBridge **Scheduler** → reconciler; API Gateway REST (IAM) → control Lambda |
| Queues | SQS ingest, SQS backfill (low priority), SQS DLQ (SSE-SQS) |
| Status | DynamoDB `ingestion_status` (single table, §6) |
| Cache | DynamoDB `embedding_cache` |
| Staging | S3 `ingest-staging-<env>` (versioned, KMS) |
| Embeddings | Amazon Bedrock, Titan Text Embeddings v2, 1024-d, normalized (model id + dim configurable) |
| OCR / large files | ECS Fargate task (arm64) + Amazon Textract async, run from Step Functions (`ecs:runTask.sync`) |
| Vector store | **Aurora PostgreSQL 16 + pgvector ≥ 0.8**, Serverless v2, via **RDS Proxy** with IAM auth, master secret in Secrets Manager |
| DB driver | `psycopg` 3 + `pgvector`; plain-SQL migrations run by a migration Lambda |
| IaC | Terraform ≥ 1.10, AWS provider (pin the org's major; 6.x preferred), S3 backend with `use_lockfile = true` |
| IaC quality | `terraform fmt -check`, `validate`, `tflint`, `checkov` (or `trivy config`), native `terraform test`, `terraform-docs` |
| Tests | `pytest`, `moto`, `testcontainers` with `pgvector/pgvector:pg16` |

## 4. End-to-end flow

```
articles stream ─(Pipes filter)─┐
reconciler (hourly) ────────────┼─▶ SQS ingest ─▶ dispatcher λ ─▶ Step Functions (per article version)
control API ────────────────────┘        └─▶ DLQ (maxReceiveCount 5)
  1 Claim → 2 Fetch → 3 Extract → 4 Normalize → 5 Chunk → 6 Embed → 7 Validate → 8 Index → 9 Finalize
  each step reads the previous step's staged output, writes its own, and updates ingestion_status
```

### 4.1 Pipes filter (articles stream → SQS)
```json
{"eventName": ["INSERT", "MODIFY"], "dynamodb": {"NewImage": {"status": {"S": ["STAGED"]}}}}
{"eventName": ["REMOVE"]}
```
The Pipe has its own DLQ (`dead_letter_config`), `maximum_retry_attempts = 5`, `starting_position = LATEST`. Use a Pipe input transformer, or have the dispatcher map the stream record to `IngestMessage`.

### 4.2 Queue message (`IngestMessage`, Pydantic)
```python
class IngestMessage(BaseModel):
    article_id: str
    process_id: str | None
    event: Literal["UPSERT", "REMOVE"]
    s3_uri: str | None
    s3_version_id: str | None
    content_sha256: str | None          # from the producer row; no S3 HEAD needed
    trigger: Literal["STREAM", "RECONCILER", "MANUAL", "BACKFILL"]
    requested_by: str | None = None
    force: bool = False
    from_stage: StepName | None = None
    priority: Literal["normal", "low"] = "normal"
```

### 4.3 Dispatcher
- SQS batch with partial batch responses; reserved concurrency from a variable (sized to the Bedrock TPM quota).
- `StartExecution` name = `{article_id}#{content_sha256[:16]}#{pipeline_version}` (sanitized, ≤ 80 chars). `ExecutionAlreadyExists` → ack and drop. Replays (`force`/`from_stage`) append `#r{short_run_id}`.

### 4.4 Steps

| # | Step | Reads | Writes to staging | `LATEST.status` after |
|---|---|---|---|---|
| 1 | Claim | `LATEST` | – | CLAIMED / SKIPPED_UNCHANGED / exit |
| 2 | Fetch | raw object (by `s3_version_id`) | – | FETCHING |
| 3 | Extract | raw object | `extracted/content.json` | EXTRACTING |
| 4 | Normalize | `extracted/` | `normalized/document.json` | NORMALIZING |
| 5 | Chunk | `normalized/` | `chunks/chunks.jsonl` | CHUNKING |
| 6 | Embed | `chunks/` + cache | `embeddings/vectors.parquet` | EMBEDDING |
| 7 | Validate | `chunks/`, `embeddings/` | `_manifest.json` | STAGED |
| 8 | Index | validated `embeddings/` + `chunks/` | – | INDEXING |
| 9 | Finalize | – | – | INDEXED |

- **Claim** (result recorded on `RUN#`; no step item): REMOVE → delete path. No `LATEST` → create (`attribute_not_exists`), `doc_version=1`. Live lease held by another run → exit. Same `content_sha256` + `pipeline_version` and not `force` → `RUN#` outcome `SKIPPED_UNCHANGED` (still counts for the process, §6.4). Otherwise `doc_version += 1`, take the lease.
- **Fetch**: `GetObject` with `VersionId`; verify SHA-256 == `content_sha256`. Because the producer verifies before writing the row, `NoSuchKey` or a hash mismatch is **permanent** (`E_SOURCE_MISSING`, `E_SOURCE_CHANGED`). No waiting retry. Sniff the MIME type; route large or textless files to Fargate.
- **Extract**: HTML → trafilatura; PDF → pdfplumber (MIT; layout, pages, tables) — not PyMuPDF (AGPL), per the open-source-only policy; scanned → Textract async on Fargate, checkpointing pages to staging; DOCX → mammoth.
- **Normalize** (deterministic for input + `pipeline_version`): NFKC, ftfy, strip control and zero-width characters, collapse whitespace, de-hyphenate, drop repeated headers/footers/page numbers; canonical blocks (`heading|paragraph|list|table|code`, `heading_path`, `page`); join article/process metadata; ISO-8601 UTC dates. Don't lowercase, stem or remove stopwords. Empty → `SKIPPED` `E_EMPTY_TEXT`.
- **Chunk**: structure-aware recursive split (heading → paragraph → sentence), 400–512 tokens with the model's tokenizer, ~64-token overlap within a section, tables/code atomic, merge chunks < 64 tokens. `embed_text = f"{title} › {' > '.join(heading_path)}\n\n{text}"`. Parent–child chunking behind a flag (child ~256, parent ~1,200 tokens). `chunk_id = sha256(f"{article_id}:{doc_version}:{ordinal}")`.
- **Embed**: cache lookup by `sha256(embed_text)#model_id`; only misses go to Bedrock (bounded concurrency, retry on throttling); write `vectors.parquet`.
- **Validate**: vector count == chunk count; dimension matches the target table; no empty or oversized chunks; unique `chunk_id`; required metadata present. Write `_manifest.json` (`passed`). Fail → `FAILED_PERMANENT` `E_VALIDATION_<CHECK>`.
- **Index**: only if the manifest passed; transactional write + swap (§7.2).
- **Finalize**: release the lease, `INDEXED`, process roll-up (§6.4), emit EventBridge `article.indexed`.

## 5. Staging layout
```
s3://ingest-staging-<env>/<article_id>/v<doc_version>/
  extracted/content.json  normalized/document.json  chunks/chunks.jsonl
  embeddings/vectors.parquet  _manifest.json
```
Write-once per version: write to `_tmp/<run_id>/…`, then `CopyObject` to the final key and record the ETag on the step item. Lifecycle: older versions → Glacier IR after 30 days; expiry configurable.

## 6. Status tracking: DynamoDB `ingestion_status` (single table)

### 6.1 Items
| PK | SK | Attributes | TTL |
|---|---|---|---|
| `article_id` | `LATEST` | `status, stage, run_id, doc_version, content_sha256, pipeline_version, process_id, lease_owner, lease_expires_at, staging_prefix, chunk_count, error_class, error_code, updated_at` + GSI keys | – |
| `article_id` | `RUN#<run_id>` | `trigger, requested_by, from_stage, claim (NEW/CHANGED/UNCHANGED/DELETE), outcome, pipeline_version, embedding_model, execution_arn, started_at, ended_at` | ~1 yr |
| `article_id` | `RUN#<run_id>#STEP#<nn>_<NAME>` | `step_status, attempt, attempts (last 5), started_at, ended_at, duration_ms, input_uri, output_uri, output_etag, metrics{}, error_class, error_code, error_msg (≤1 KB)` | ~90 d |
| `PROCESS#<process_id>` | `SUMMARY` | `indexed, skipped, failed, status, updated_at` | – |

`run_id` = ULID. Retries update the same step item; a new run gets a new `run_id`.

### 6.2 Sparse GSIs
`gsi1-status` (status / updated_at) on LATEST · `gsi2-process` (process_id / status#article_id) on LATEST · `gsi3-lease` (lease_bucket / lease_expires_at) on LATEST while running · `gsi4-stepfail` (STEPFAIL#<STEP> / updated_at) on FAILED step items.

### 6.3 Step write protocol (one decorator used by every step)
1. **Start:** a transaction updates `LATEST` (stage; condition: `run_id = :run AND lease_expires_at > :now AND stage IN (:prev)`) and puts the step item STARTED with `attempt + 1`. Condition failure → stale → the execution ends quietly.
2. **Work:** read `input_uri`; write output via temp key + copy.
3. **Success:** step item SUCCEEDED + timings, URIs, ETag, metrics; extend the lease.
4. **Error:** classify; step item FAILED (attempts list capped at 5; set gsi4 keys); re-raise so Step Functions `Retry`/`Catch` can match on the error name.
5. `Catch` → `MarkFailed`: `LATEST` = `FAILED_RETRYABLE` (with `next_retry_at`) or `FAILED_PERMANENT`; permanent → SNS alarm.

Records per run (assert in tests): clean 9 · first run 10 · skipped 1 · failed at step N → N · replay 1 + steps re-run · delete 2.

### 6.4 Process roll-up
On every move into a terminal state (`INDEXED`, `SKIPPED_*`, `FAILED_PERMANENT`), in the **same transaction** as the `LATEST` update (condition: previous status non-terminal), `ADD` the matching counter on `PROCESS#<id>/SUMMARY`. Leaving `FAILED_PERMANENT` on replay → `ADD failed -1` in that transaction.
Completion check (after each terminal move, and in the reconciler): read the producer's `process` item. If it is settled and `indexed + skipped + failed == registered_count`, set `COMPLETE` or `COMPLETE_WITH_ERRORS` and emit `process.ingested`. The reconciler covers processes that settle after their last article finished.

## 7. pgvector

### 7.1 Schema (`db/migrations/NNN_*.sql`)
```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE chunks_titan2_1024 (
  chunk_id text PRIMARY KEY, article_id text NOT NULL, doc_version int NOT NULL, ordinal int NOT NULL,
  parent_id text, process_id text, tenant_id text, is_active boolean NOT NULL DEFAULT false,
  text text NOT NULL, embed_text text NOT NULL, heading_path text[], page_start int, page_end int,
  token_count int NOT NULL, metadata jsonb NOT NULL DEFAULT '{}',
  content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  embedding halfvec(1024) NOT NULL, embedding_model text NOT NULL, pipeline_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON chunks_titan2_1024 USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64) WHERE is_active;
CREATE INDEX ON chunks_titan2_1024 (article_id, doc_version);
CREATE INDEX ON chunks_titan2_1024 USING gin (content_tsv) WHERE is_active;
CREATE INDEX ON chunks_titan2_1024 (tenant_id) WHERE is_active;
CREATE OR REPLACE VIEW chunks_active AS SELECT * FROM chunks_titan2_1024 WHERE is_active;
```
At query time: `SET LOCAL hnsw.ef_search = 100; SET LOCAL hnsw.iterative_scan = relaxed_order;`. A reference vector search and a hybrid search (vector + `ts_rank`, reciprocal rank fusion) go in `src/ingest/search/`, for evaluation only.

### 7.2 Index: transactional write + swap
```
BEGIN;  SELECT pg_advisory_xact_lock(hashtext(:article_id));
  COPY into temp table → INSERT … ON CONFLICT (chunk_id) DO UPDATE      -- version N+1, is_active=false
  verify count = :chunk_count
  UPDATE … SET is_active = (doc_version = :v) WHERE article_id = :a;
  DELETE … WHERE article_id = :a AND doc_version < :v;
COMMIT;
```
Delete path: `DELETE … WHERE article_id = :a`. Connect through RDS Proxy (IAM token), with short transactions and `statement_timeout`.

### 7.3 Model change and rebuild
A new table per model/dimension (migration) → `POST /v1/reindex {from_stage: "EMBED", target_table}` re-embeds from staged `chunks/` → golden-set evaluation → `CREATE OR REPLACE VIEW chunks_active` to switch (switch back to roll back). A full rebuild uses `from_stage: "INDEX"`. For bulk loads, drop the HNSW index, load, then `CREATE INDEX CONCURRENTLY` with a raised `maintenance_work_mem`.

## 8. Terraform: reusable modules

### 8.1 Repository layout
```
ingestion-pipeline/
  src/ingest/ … db/migrations/ … tests/ … scripts/build_lambda.sh  scripts/build_image.sh
  terraform/
    modules/                     # reusable, service-agnostic building blocks (§8.2)
      kms_key/  s3_bucket/  dynamodb_table/  sqs_queue/  lambda_function/  lambda_sqs_trigger/
      eventbridge_pipe/  scheduler_schedule/  step_function/  ecs_fargate_task/  ecr_repository/
      aurora_postgres/  rds_proxy/  vpc_endpoints/  security_group/  api_gateway_rest/  alarms/
    stacks/
      ingestion/                 # composition module: wires the building blocks for this service (§8.3)
    envs/
      dev/   backend.tf providers.tf main.tf variables.tf terraform.tfvars outputs.tf
      prod/  …
    tests/                       # *.tftest.hcl
```

Rules for every module in `modules/`:
- One responsibility, **no provider blocks**, `versions.tf` with `required_providers`, input `variable`s with `type`, `description` and `validation` where useful, explicit `output`s. `tags` passed through.
- Secure by default: KMS key ARN is a **required** input wherever data is stored; public access off; TLS-only policies; PITR, versioning and log retention on.
- No hard-coded names, accounts or regions. Callers pass `name`/`name_prefix`.
- IAM policies are passed in as JSON (`policy_documents = list(string)`), built by the caller with `aws_iam_policy_document`. Modules create roles but never invent permissions.
- Each module ships `README.md` (terraform-docs), `examples/basic`, and a `tests/*.tftest.hcl` plan test.
- Version modules with git tags (`modules/<name>/vX.Y.Z`) so the **producer can use the same modules**. Callers reference `?ref=` tags; within this repo, relative paths are fine until the modules move to a shared repo or private registry.

### 8.2 Module catalog (inputs → outputs)

| Module | Key inputs | Outputs | Notes |
|---|---|---|---|
| `kms_key` | `alias`, `description`, `service_principals`, `admin_role_arns` | `key_arn`, `key_id`, `alias_arn` | rotation on; deletion window var |
| `s3_bucket` | `name`, `kms_key_arn`, `versioning`, `lifecycle_rules` (list of objects), `cors_rules`, `force_destroy`, `bucket_policy_statements` | `bucket_name`, `bucket_arn` | BPA ×4, BucketOwnerEnforced, TLS-only policy merged with caller statements |
| `dynamodb_table` | `name`, `hash_key`, `range_key`, `attributes`, `global_secondary_indexes` (list of objects), `stream_view_type`, `ttl_attribute`, `kms_key_arn`, `deletion_protection` | `table_name`, `table_arn`, `stream_arn`, `index_arns` | PAY_PER_REQUEST, PITR on |
| `sqs_queue` | `name`, `visibility_timeout`, `max_receive_count`, `dlq_retention_days`, `fifo` | `queue_arn`, `queue_url`, `dlq_arn`, `dlq_url` | creates the main queue + DLQ + redrive; SSE-SQS |
| `lambda_function` | `name`, `handler`, `runtime`, `architecture`, `package` (object: `zip_path` or `s3_bucket`/`s3_key`, or `image_uri`), `memory`, `timeout`, `env`, `policy_documents`, `vpc_config` (optional), `reserved_concurrency`, `log_retention_days`, `kms_key_arn` | `function_arn`, `function_name`, `role_arn`, `role_name`, `invoke_arn` | creates role + log group first; X-Ray active; AWSLambdaBasicExecutionRole (+ VPC access role when in VPC) |
| `lambda_sqs_trigger` | `function_name`, `queue_arn`, `batch_size`, `batching_window`, `max_concurrency` | `mapping_uuid` | `ReportBatchItemFailures` always on |
| `eventbridge_pipe` | `name`, `source_stream_arn`, `target_queue_arn`, `filter_patterns` (list of JSON), `batch_size`, `max_retries`, `dlq_arn`, `input_template` | `pipe_arn`, `role_arn` | builds its own least-privilege role from source/target ARNs |
| `scheduler_schedule` | `name`, `schedule_expression`, `target_arn`, `input`, `flexible_window_minutes` | `schedule_arn` | role scoped to the target |
| `step_function` | `name`, `definition` (string), `type`, `policy_documents`, `log_level`, `kms_key_arn` | `state_machine_arn`, `role_arn` | CloudWatch logging + X-Ray; callers render ASL with `templatefile` |
| `ecs_fargate_task` | `name`, `image_uri`, `cpu`, `memory`, `architecture`, `env`, `policy_documents`, `subnet_ids`, `security_group_ids` | `cluster_arn`, `task_definition_arn`, `task_role_arn`, `execution_role_arn` | ARM64, log group, no public IP |
| `ecr_repository` | `name`, `kms_key_arn`, `keep_last_n` | `repository_url`, `repository_arn` | scan on push, immutable tags |
| `aurora_postgres` | `name`, `engine_version`, `min_acu`, `max_acu`, `instance_count`, `subnet_ids`, `allowed_security_group_ids`, `kms_key_arn`, `deletion_protection`, `parameters` | `cluster_arn`, `cluster_endpoint`, `reader_endpoint`, `master_secret_arn`, `security_group_id` | Serverless v2, `manage_master_user_password`, IAM auth on, Performance Insights, backups, log exports |
| `rds_proxy` | `name`, `cluster_id`, `secret_arns`, `subnet_ids`, `allowed_security_group_ids`, `iam_auth` | `endpoint`, `proxy_arn`, `proxy_resource_id`, `security_group_id` | `require_tls`, IAM auth REQUIRED |
| `vpc_endpoints` | `vpc_id`, `subnet_ids`, `route_table_ids`, `services` (list) | `endpoint_ids`, `security_group_id` | gateway: s3, dynamodb; interface: sqs, states, bedrock-runtime, secretsmanager, logs, ecr.api, ecr.dkr, textract, sts, events |
| `security_group` | `name`, `vpc_id`, `ingress`, `egress` (lists of objects) | `id` | default deny |
| `api_gateway_rest` | `name`, `lambda_invoke_arn`, `lambda_function_name`, `stage_name`, `throttle_rate`, `throttle_burst`, `waf_acl_arn`, `log_retention_days` | `api_id`, `invoke_url`, `execution_arn`, `invoke_policy_arn` | `{proxy+}` + root `ANY`, AWS_IAM, access logs, X-Ray, redeploy triggers (same module the producer uses) |
| `alarms` | `topic_name`, `email`, `alarms` (list of objects: metric, threshold, periods, comparison) | `topic_arn`, `alarm_arns` | one place for alarm wiring |

### 8.3 Composition: `stacks/ingestion`
Calls the modules above to create:

| Resource | Module | Settings |
|---|---|---|
| CMK | `kms_key` | used by every data store in this stack |
| Staging bucket | `s3_bucket` | versioned; lifecycle noncurrent → GLACIER_IR 30 d; expiry var |
| `ingestion_status` | `dynamodb_table` | PK `pk`, SK `sk`; 4 sparse GSIs (§6.2); TTL `expires_at_epoch` |
| `embedding_cache` | `dynamodb_table` | PK `cache_key`; TTL |
| Ingest, backfill, pipe-DLQ queues | `sqs_queue` ×3 | ingest visibility ≥ 6× dispatcher timeout; maxReceive 5 |
| Pipe | `eventbridge_pipe` | source = producer `articles` stream ARN; filters §4.1; target ingest queue |
| Lambdas | `lambda_function` ×N | dispatcher, claim, fetch, extract, normalize, chunk, embed, validate, index, finalize, mark_failed, control_api, reconciler, drift_check, migrate |
| SQS triggers | `lambda_sqs_trigger` ×2 | dispatcher ← ingest (normal), dispatcher ← backfill (low `max_concurrency`) |
| Ingest workflow | `step_function` | `templatefile("asl/ingest.asl.json.tftpl", { lambda arns, ecs task, retry config })` |
| Reindex workflow | `step_function` | Distributed Map over LATEST items → backfill queue |
| Large-doc worker | `ecr_repository` + `ecs_fargate_task` | image built in CI (`scripts/build_image.sh`); invoked with `ecs:runTask.sync` |
| Database | `aurora_postgres` + `rds_proxy` | private subnets; only Lambda SGs may connect; IAM auth |
| Network | `security_group` ×N + `vpc_endpoints` | Lambdas that touch Postgres run in the VPC; endpoints so they need no NAT |
| Control API | `api_gateway_rest` | → control_api Lambda |
| Schedules | `scheduler_schedule` ×2 | reconciler `rate(1 hour)`, drift check `cron(0 3 ? * SUN *)` |
| Alarms | `alarms` | §12 |

Least-privilege policy documents per function live in `stacks/ingestion/iam.tf`, one `aws_iam_policy_document` per function:
- **dispatcher:** `states:StartExecution`; SQS consume.
- **claim/finalize/mark_failed:** status table + process table read (producer) + embedding nothing.
- **fetch/extract:** producer raw bucket `s3:GetObject*` on `raw/*` + producer CMK `kms:Decrypt`; staging bucket put/get.
- **embed:** `bedrock:InvokeModel` on the model ARN, cache table, staging.
- **index:** `rds-db:connect` on the proxy user, staging read.
- **control_api / reconciler:** status table, producer `articles` read (`gsi_updated`), SQS send, `states:StartExecution` on the reindex machine.

### 8.4 Inputs from the producer (data sources, read-only)
```hcl
data "aws_dynamodb_table" "articles" { name = var.producer_articles_table }   # .stream_arn, .arn
data "aws_dynamodb_table" "process"  { name = var.producer_process_table }
data "aws_s3_bucket" "raw"           { bucket = var.producer_raw_bucket }
data "aws_kms_key" "producer"        { key_id = var.producer_kms_key_arn }
```
Never manage producer resources here. Optionally switch to `terraform_remote_state` or SSM parameters if the platform team prefers.

### 8.5 Envs, state, packaging
- `envs/<env>/main.tf` calls `stacks/ingestion` with env values only. Backend: S3 key `ingestion/<env>/terraform.tfstate`, `encrypt = true`, `use_lockfile = true`. Provider `default_tags`: service, env, owner, cost-center.
- **Lambda packaging** (CI, before `plan`; never `local-exec` during plan): `scripts/build_lambda.sh <group>` builds deterministic zips with `uv pip install . --target … --python-platform aarch64-manylinux2014 --python-version 3.12` (+ group extras), normalized mtimes, `zip -X`. Two groups: `core` (light: dispatcher, claim, validate, index, finalize, control, reconciler, migrate) and `docs` (extract, normalize, chunk, embed, with PyMuPDF, trafilatura, mammoth, tokenizer). If a group exceeds the 250 MB unzipped limit, use a container image via `ecr_repository` with `package.image_uri`.
- Prod: CI uploads artifacts to an artifacts bucket at `ingestion/<git_sha>/<group>.zip`; Terraform takes `artifact_version = git_sha`.
- **Migrations:** CI step after apply invokes the `migrate` Lambda (`aws lambda invoke`), which applies `db/migrations` in order and records them in `schema_migrations`. Not run from Terraform.

## 9. Error taxonomy (enum + classifier)

| Code | Example | Class | Policy |
|---|---|---|---|
| `E_THROTTLED` | Bedrock `ThrottlingException` | transient | SFN Retry 2 s, ×2, full jitter, max 6 |
| `E_UPSTREAM_5XX` / `E_TIMEOUT` | ServiceUnavailable | transient | 3 retries, then FAILED_RETRYABLE |
| `E_DB_CONFLICT` / `E_DB_CONN` | serialization failure, proxy busy | transient | retry |
| `E_SOURCE_MISSING` / `E_SOURCE_CHANGED` | NoSuchKey / hash ≠ row | permanent | DLQ + alarm (producer contract broken) |
| `E_UNREADABLE` | encrypted or corrupt PDF | permanent | DLQ + alarm |
| `E_EMPTY_TEXT` | no text after OCR | skip | route to OCR once, then SKIPPED |
| `E_TOO_LARGE` | beyond Lambda limits | route | Fargate |
| `E_VALIDATION_*` | count or dim mismatch | permanent | blocked before index |
| `E_BUG` | anything unexpected | permanent | DLQ + alarm; never retried forever |

## 10. Control API (REST, IAM; operator role for writes)

| Endpoint | Body | Behavior |
|---|---|---|
| `POST /v1/ingest` | `article_ids[], force?, from_stage?, dry_run?` | enqueue MANUAL |
| `POST /v1/processes/{id}/ingest` | `only_failed?, dry_run?` | via `gsi2-process` |
| `POST /v1/reindex` | `filter{since?, process_id?, pipeline_version?}, target_table, from_stage, dry_run?` | reindex state machine → backfill queue |
| `POST /v1/dlq/redrive` | `error_code?, max?, dry_run?` | selective redrive |
| `GET /v1/articles/{id}/status` | – | LATEST + last 5 runs + latest run's steps + execution link |
| `GET /v1/processes/{id}/summary` | – | pipeline counters + producer counts + failures by `error_code` |

Resume logic: last SUCCEEDED step of the previous run whose `output_uri` still has the recorded ETag → start at the next step; fall back to Fetch.

## 11. Reconciler (hourly, idempotent)
1. `gsi3-lease` expired → re-queue with resume.
2. `gsi1-status` FAILED_RETRYABLE past `next_retry_at`: `attempt < 5` → re-queue; else FAILED_PERMANENT + alarm.
3. `gsi1-status` STAGED older than 30 min → re-queue `from_stage=INDEX`.
4. Producer `articles.gsi_updated` for today and yesterday: no `LATEST`, or `content_sha256` differs → enqueue RECONCILER.
5. Process completion check (§6.4) for SUMMARY items not yet COMPLETE whose producer process is settled.
6. Weekly drift check: `chunks_active` count per article vs `LATEST.chunk_count` → repair from staging.
Metrics: `requeued`, `escalated`, `drift_repaired`, `processes_completed`.

## 12. Observability & security
- Alarms (via `alarms` module): step failure rate, p95 step duration vs baseline, ingest queue oldest-message age, any DLQ > 0 (ingest, pipe, registrar-style), freshness lag (`indexed_at − producer updated_at`), Aurora CPU/ACU/buffer-cache hit ratio, RDS Proxy connection saturation, Bedrock throttles.
- Logs: JSON with `article_id, run_id, process_id, step, attempt`; X-Ray everywhere.
- Security: KMS on every store; VPC endpoints; Lambdas and Fargate tasks in private subnets with no public egress (add NAT only if a dependency needs it); one IAM role per function; DB users `ingest_writer` (Index, migrate) and `ingest_reader` (search/eval).

## 13. Repository layout (Python)
```
src/ingest/
  models/ status/ staging/ steps/ extract/ chunking/ embedding/ vectorstore/ search/
  control_api/ reconciler/ errors.py config.py
asl/ingest.asl.json.tftpl  asl/reindex.asl.json.tftpl
db/migrations/
fargate/Dockerfile          # large-doc worker
tests/unit tests/integration tests/e2e tests/golden
```

## 14. Open questions (ask before Phase 1)
1. Existing VPC (IDs for private subnets and route tables), or should the stack create one?
2. Tenant/ACL filtering at query time? (Shapes `tenant_id`.)
3. Metadata-only changes: ignore, or update chunk metadata in place without re-embedding?
4. Volume: articles/day, peak burst, largest file, average chunks per article (sizes Aurora ACUs, Bedrock quota, Fargate).
5. Retention of staging and vectors after an article is deleted.
6. Shared module location: keep `terraform/modules` here, or publish to a shared repo/private registry for the producer to consume?

## 15. Phases and gates

**Phase 0: Foundations and modules**
- [ ] Repo, CI (ruff, mypy, pytest, terraform fmt/validate/tflint/checkov/test)
- [ ] Modules: `kms_key`, `s3_bucket`, `dynamodb_table`, `sqs_queue`, `lambda_function`, `lambda_sqs_trigger`, each with README, example and plan test
- [ ] Pydantic models; `db/migrations/001_init.sql`; testcontainers fixture
- Gate: module interfaces reviewed

**Phase 1: Thin end-to-end path**
- [ ] Modules: `eventbridge_pipe`, `step_function`, `security_group`, `vpc_endpoints`, `aurora_postgres`, `rds_proxy`
- [ ] Stack: Pipe → ingest → dispatcher → 9 steps (HTML/TXT/DOCX), staging, status writes, Bedrock, Index with swap; migrate Lambda
- Gate: 1,000 sample articles from the producer indexed in dev; record counts per run match §6.3

**Phase 2: Hardening**
- [ ] Error taxonomy, Retry/Catch, MarkFailed, leases, dedupe, Validate gate
- [ ] Modules: `api_gateway_rest`, `scheduler_schedule`, `alarms`; control API; reconciler; delete path; process roll-up against producer counts
- Gate: all acceptance tests in §16 pass in dev

**Phase 3: Scale and quality**
- [ ] Modules: `ecr_repository`, `ecs_fargate_task`; Textract path with checkpoints
- [ ] Embedding cache; parent–child flag; golden-set evaluation in CI (recall@5/@10, MRR)
- [ ] Reindex state machine (Distributed Map); blue/green view swap; HNSW build-after-load
- Gate: backfill complete; golden-set baseline recorded

**Phase 4: Operate**
- [ ] Prod env (deletion protection, `prevent_destroy` on data stores), dashboards, tuned alarms, `docs/RUNBOOK.md`
- [ ] Tag module releases; producer migrated onto the shared modules (separate PR in the producer repo)

## 16. Acceptance tests

| ID | Scenario | Expect |
|---|---|---|
| UC1 | New article from the producer | LATEST INDEXED; 10 records; rows active in `chunks_active` |
| UC2 | Content changed (new upload) | `doc_version` 2; v1 served until commit; only v2 active after; cache hits for unchanged chunks |
| UC3 | Re-registered unchanged / metadata PATCH | 1 record (`RUN#` SKIPPED_UNCHANGED); no Bedrock or DB writes; counted as skipped for the process |
| UC4 | Bedrock throttled once | same step item `attempt=2` SUCCEEDED |
| UC5 | Encrypted PDF → fix → redrive by `error_code` | first run FAILED_PERMANENT; new run INDEXED; process `failed −1`, `indexed +1` |
| UC6 | Kill Chunk after its staging write | reconciler resumes at Chunk; no duplicate `chunk_id`s |
| UC7 | Stream events dropped | reconciler enqueues from `gsi_updated` |
| UC8 | Article deleted in producer | rows gone; LATEST DELETED; 2 records |
| UC9 | Model change | new table filled from `chunks/`; view switched; rollback works |
| UC10 | Rebuild | truncate, `from_stage=INDEX` restores row counts with zero Bedrock calls |
| UC11 | Producer process settles | SUMMARY COMPLETE / COMPLETE_WITH_ERRORS when counts reach `registered_count`; event emitted |
| UC12 | 600-page scanned PDF | Fargate + Textract; INDEXED; no Lambda timeout |
| DUP | Same stream record delivered 3× | one execution, one run |
| CONC | Two runs for one article | one wins the lease; the other exits quietly |
| TF | Every module | `terraform test` passes; checkov clean; example applies and destroys in a sandbox account |

## 17. Rules of engagement
- Don't modify producer resources or code; read-only IAM on its tables, bucket and key.
- Status lives only in `ingestion_status`; no nested step maps; no per-attempt items.
- No unbounded retries; unknown errors are permanent.
- Modules stay generic: nothing ingestion-specific inside `terraform/modules/`; service wiring lives in `stacks/ingestion`.
- Build artifacts before `plan`; no `null_resource`/`local-exec` builds.
- Short Postgres transactions with `statement_timeout`; timeouts on every external call.
- Ask the §14 questions first; when a decision is still open, put it behind a variable and note it in the PR.