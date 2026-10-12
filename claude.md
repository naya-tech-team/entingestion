# Handoff: Producer service (Python Lambda + API Gateway, Terraform)

You are building the **producer** for an article ingestion platform on AWS. Clients use its API to register a batch ("process"), upload files to S3, and have each verified file recorded in the DynamoDB `articles` table. A separate ingestion pipeline consumes the `articles` stream; your output is its input, so the contracts in §4–§6 are fixed.

Read the whole brief first. Build in the phases in §12, one PR per phase, and stop at each gate for review.

> **Reference implementation.** If `producer-service.zip` is available, its `src/producer/`, `client/` and `tests/` already implement and test this behaviour (15 passing moto tests). Reuse that application code; **replace its CDK `infra/` with Terraform** as specified here. If the zip is not available, implement from this brief.

---

## 1. Requirements

1. **A row never points at a missing or corrupt file.** The `articles` row is written only after S3 has the object and its size and SHA-256 match what the client declared.
2. **Clients upload straight to S3** through presigned PUT URLs. File bytes never pass through API Gateway or Lambda (API Gateway caps payloads at 10 MB, Lambda at 6 MB).
3. **Idempotent:** duplicate S3 events, client retries and out-of-order events can't create duplicates, double-count, or roll an article back to an older file.
4. **Re-sending a document updates the same article** (deterministic id from `source` + `external_id`).
5. **Exact process counts** for the ingestion pipeline: `expected_count`, `registered_count`, `rejected_count`, and `settled`.
6. **IAM-authenticated API**, least-privilege roles, KMS encryption everywhere, alarms on failures.
7. **All infrastructure in Terraform.** No CDK, no console changes, no ClickOps.

## 2. Tech stack

| Concern | Choice |
|---|---|
| Language | Python 3.12 (Lambda runtime `python3.12`, `arm64`) |
| Packaging | `uv`; Pydantic v2; AWS Lambda Powertools v3 (Logger, Metrics, Tracer, event handler, batch) |
| Quality | `ruff`, `mypy --strict`, `pytest` + `moto` |
| IaC | Terraform ≥ 1.10, AWS provider (pin the major your org uses; 6.x preferred), S3 remote state with `use_lockfile = true` |
| IaC quality | `terraform fmt -check`, `terraform validate`, `tflint`, `checkov` (or `trivy config`), native `terraform test` |
| API | API Gateway **REST API** (regional), `{proxy+}` → one Lambda, `AWS_IAM` auth |
| Async | S3 ObjectCreated → SQS (+ DLQ) → registrar Lambda |
| Data | DynamoDB (on-demand, PITR, KMS), S3 (versioned, KMS) |

Why REST and not HTTP API: REST supports AWS WAF, per-method throttling, request validation and usage plans, which an enterprise API usually needs. If none of those is required, HTTP API with IAM auth is cheaper. Keep the choice behind a module so it can change.

## 3. Architecture

```
client ─POST /v1/processes────────────────▶ API GW (IAM) ─▶ api λ ─▶ process table
client ─POST /v1/processes/{id}/uploads───▶ api λ ─▶ producer_uploads (PENDING) ─▶ presigned PUT URL (SHA-256 signed)
client ─PUT file──────────────────────────────────────────────────────────────────▶ S3 raw/{article_id}/{upload_id}/{filename}
S3 ObjectCreated(raw/) ─▶ SQS ─▶ registrar λ ─▶ verify size + SHA-256 ─▶ articles row (STAGED) ─▶ DynamoDB stream ─▶ ingestion
client ─POST /v1/processes/{id}/close─────▶ api λ ─▶ process CLOSED, expected_count set
```

## 4. API contract (all JSON, `AWS_IAM` auth)

| Method | Path | Body | Success |
|---|---|---|---|
| POST | `/v1/processes` | `name` (1–200), `source` (`^[a-z0-9][a-z0-9._-]{0,63}$`), `expected_count?` (≥0), `metadata?` (≤50 string pairs) | 201 Process |
| GET | `/v1/processes/{process_id}` | – | 200 Process |
| POST | `/v1/processes/{process_id}/close` | `expected_count?` (default: current `registered_count`) | 200 Process |
| POST | `/v1/processes/{process_id}/uploads` | `filename` (no path separators/control chars, ≤255), `content_type` (allow-list), `size_bytes` (>0, ≤ max), `sha256` (64 hex), `external_id?` (≤256), `title?`, `metadata?` | 201 UploadTicket |
| GET | `/v1/processes/{process_id}/articles` | `?limit=1..500&next_token=` | 200 `{items, next_token}` |
| GET | `/v1/uploads/{upload_id}` | – | 200 Upload |
| GET | `/v1/articles/{article_id}` | – | 200 Article |
| PATCH | `/v1/articles/{article_id}` | `title?`, `metadata?` (at least one) | 200 Article |
| DELETE | `/v1/articles/{article_id}` | – | 204 |

- `UploadTicket = {upload_id, article_id, process_id, method:"PUT", url, headers, expires_at}`. The client must send exactly `headers` with the PUT.
- Errors: `404` not found, `409` process not OPEN, `422` validation `{message, errors:[{loc,msg}]}`. Unknown body fields → 422.
- `created_by` / `requested_by` = caller IAM ARN from `requestContext.identity.userArn`.
- Default content-type allow-list: `text/html`, `text/plain`, `text/markdown`, `application/pdf`, DOCX (`application/vnd.openxmlformats-officedocument.wordprocessingml.document`). Configurable via env.

## 5. Data model

### `process` (PK `process_id`)
`process_id` (`prc_<ULID>`), `name`, `source`, `status` (`OPEN|CLOSED`), `expected_count?`, `registered_count`, `rejected_count`, `metadata`, `created_by`, `created_at`, `updated_at`, `closed_at?`.
API adds computed `settled = status==CLOSED && expected_count != null && registered_count + rejected_count >= expected_count`.

### `articles` (PK `article_id`, **Streams NEW_AND_OLD_IMAGES**)
`article_id`, `process_id` (latest), `first_process_id`, `source`, `external_id?`, `title?`, `filename`, `content_type`, `s3_uri`, `s3_key`, `s3_version_id?`, `content_sha256` (hex), `size_bytes`, `upload_id`, `status` (`STAGED`), `metadata`, `created_at`, `updated_at`, `updated_date` (YYYY-MM-DD).
- `article_id` = `art_` + `uuid5(NAMESPACE, f"{source}\x1f{external_id}").hex` when `external_id` is given, else `art_<ULID>`. NAMESPACE is a config constant; never change it once in use.
- GSI `gsi_process`: `process_id` / `updated_at`, projection ALL.
- GSI `gsi_updated`: `updated_date` / `updated_at`, projection INCLUDE (`content_sha256`, `process_id`, `upload_id`). The ingestion reconciler uses this.

### `producer_uploads` (PK `upload_id`, TTL `expires_at_epoch`)
Upload tickets: `upload_id` (`upl_<ULID>`), `article_id`, `process_id`, `source`, `s3_key`, `filename`, `content_type`, `size_bytes`, `sha256`, `external_id?`, `title?`, `metadata?`, `status` (`PENDING|REGISTERING|REGISTERED|SUPERSEDED|REJECTED`), `reason?`, `requested_by`, `created_at`, `finished_at?`. PENDING TTL 24 h; finished uploads kept 30 days.
Also holds membership markers `upload_id = "MEMBER#{process_id}#{article_id}"` (`kind = "membership"`) for exact counting.

### S3 raw bucket
Key per upload: `raw/{article_id}/{upload_id}/{filename}`. Never overwritten.

## 6. Core logic (must match exactly)

### 6.1 Issue an upload ticket
1. Process must exist and be OPEN (else 409). Validate content type and size.
2. Compute `article_id` (§5), new `upload_id` (ULID), key.
3. Put the `producer_uploads` item as PENDING (`attribute_not_exists(upload_id)`).
4. Presign `put_object` with `ContentType`, `ChecksumSHA256` (base64 of the hex digest), and `Metadata={"upload-id": upload_id}`; TTL 900 s.
5. Return headers the client must send: `Content-Type`, `x-amz-checksum-sha256`, `x-amz-meta-upload-id`. Omit any header the signer moved into the query string.
6. **S3 client config is mandatory:** `Config(signature_version="s3v4", request_checksum_calculation="when_required", response_checksum_validation="when_required")`. Otherwise newer botocore adds a CRC32 requirement to presigned URLs and client PUTs fail. **Bundle boto3/botocore in the Lambda zip**; don't rely on the runtime's copy.

### 6.2 Register an object (registrar, per S3 event record)
1. Ignore other buckets, keys outside `raw/art_*/upl_*/…`, and `s3:TestEvent`. URL-decode keys (`unquote_plus`).
2. **Claim:** the upload must exist (else ignore). Conditional update `status IN (PENDING, REGISTERING)` → `REGISTERING`; failure = duplicate → return.
3. **Verify:** HEAD with `ChecksumMode=ENABLED`. Key matches the ticket; size == declared and ≤ max; SHA-256 == declared. Use S3's stored checksum when present and not composite (no `-N` suffix); otherwise stream-hash the object.
4. Mismatch → one transaction: upload `REJECTED` (+reason, condition `status = REGISTERING`) **and** `process ADD rejected_count 1`. Return.
5. **Apply to article:** `UpdateItem` setting every field in §5. `first_process_id`, `created_at` and (if the upload has none) `metadata` use `if_not_exists`. Condition: `attribute_not_exists(article_id) OR upload_id <= :upload_id`. On condition failure → upload `SUPERSEDED`, return. **Always write**, even if the content is unchanged, so the stream tells the pipeline the article joined this process.
6. **Count once:** transaction of `Put MEMBER#{process_id}#{article_id}` (`attribute_not_exists`) + `process ADD registered_count 1`. If the put's condition fails, it was already counted; ignore.
7. Upload → `REGISTERED` (condition `status = REGISTERING`).
8. Unexpected exceptions **raise** → the SQS message is retried (partial batch response) → DLQ after 5 receives. Business outcomes (`registered|duplicate|superseded|rejected|ignored`) return normally and emit a metric `Upload_<outcome>`.

### 6.3 Close
Conditional update `status = OPEN` → `CLOSED`, `closed_at`, `expected_count = :given` or `if_not_exists(registered_count, 0)`. Only reference attribute names actually used (DynamoDB rejects unused names). Not OPEN → 409.

### 6.4 Patch / delete
PATCH updates only `title`/`metadata` + `updated_at`/`updated_date` (condition `attribute_exists`). DELETE removes the row (condition `attribute_exists`); raw files stay for lifecycle retention.

### 6.5 Pitfalls already hit (don't repeat)
- With `boto3.resource(...).meta.client`, pass **plain Python values** to `transact_write_items`. The resource client already serializes, so pre-serialized `{"S": ...}` values get double-wrapped.
- Convert DynamoDB `Decimal` back to `int` before Pydantic validation.

## 7. Terraform design

### 7.1 Layout
```
producer-service/
  src/producer/ … client/producer_client/ … tests/ …        # Python app (reuse)
  scripts/build_lambda.sh                                    # builds dist/producer_lambda.zip
  terraform/
    modules/
      kms/            # CMK + alias, rotation, key policy (account root + service principals as needed)
      storage/        # raw bucket: versioning, SSE-KMS + bucket key, BPA, TLS-only policy, ownership, lifecycle, CORS (optional)
      tables/         # process, articles (stream + 2 GSIs), producer_uploads (TTL); PITR, SSE-KMS, deletion protection
      lambda_fn/      # reusable: function, role, log group (retention), X-Ray, env, optional reserved concurrency
      registrar/      # SQS queue + DLQ, queue policy for S3, bucket notification (prefix raw/), event source mapping
      api/            # REST API, {proxy+} ANY + root ANY with AWS_IAM, Lambda proxy integration, deployment, stage, logs, invoke policy
      alarms/         # SNS topic (+ optional email), CloudWatch alarms
    envs/
      dev/  main.tf backend.tf variables.tf terraform.tfvars outputs.tf
      prod/ …
    tests/            # *.tftest.hcl plan-time assertions
```

### 7.2 Lambda packaging (deterministic, no `local-exec` during plan)
`scripts/build_lambda.sh` (run locally and in CI **before** `terraform plan`):
```bash
rm -rf build/lambda dist && mkdir -p build/lambda dist
uv pip install . --target build/lambda \
  --python-platform aarch64-manylinux2014 --python-version 3.12 --quiet
rm -rf build/lambda/producer_client          # CLI isn't needed in Lambda
(cd build/lambda && find . -exec touch -t 202001010000 {} + && zip -qrX ../../dist/producer_lambda.zip .)
```
- Terraform takes the zip path (`var.lambda_zip_path`) with `source_code_hash = filebase64sha256(var.lambda_zip_path)`. Both functions share the one zip with different handlers.
- Prod option (preferred in CI): upload the zip to an artifacts bucket at `producer/<git_sha>.zip` and pass `s3_bucket`/`s3_key`/`source_code_hash` as variables. Implement both behind a variable.

### 7.3 Resources (per env)

| Resource | Settings |
|---|---|
| `aws_kms_key` + alias | rotation on; key policy allows the account and lets Lambda roles use it via IAM |
| `aws_s3_bucket` raw | versioning; `aws_s3_bucket_server_side_encryption_configuration` (aws:kms, bucket key); public access block (all four); ownership `BucketOwnerEnforced`; policy denying `aws:SecureTransport=false`; lifecycle: abort MPU 7 d, noncurrent → GLACIER_IR 30 d; optional CORS (PUT) from `var.cors_origins`; `force_destroy = var.env != "prod"` |
| `aws_dynamodb_table` ×3 | `PAY_PER_REQUEST`; `point_in_time_recovery`; `server_side_encryption { kms_key_arn }`; `deletion_protection_enabled = var.env == "prod"`; names `${var.table_prefix}process`, `…articles`, `…producer_uploads`; articles `stream_enabled`, `stream_view_type = "NEW_AND_OLD_IMAGES"`, GSIs per §5; uploads `ttl { attribute_name = "expires_at_epoch" }`; `lifecycle { prevent_destroy = true }` in prod |
| `aws_lambda_function` api | `producer.api.lambda_handler`, 512 MB, 29 s, arm64, python3.12, `tracing_config { mode = "Active" }` |
| `aws_lambda_function` registrar | `producer.registrar.lambda_handler`, 1024 MB, 300 s |
| `aws_cloudwatch_log_group` | `/aws/lambda/<name>`, retention 30 d (dev) / 90 d (prod), created before the functions (`depends_on`) |
| Env vars | `PROCESS_TABLE`, `ARTICLES_TABLE`, `UPLOADS_TABLE`, `RAW_BUCKET`, `RAW_PREFIX=raw/`, `POWERTOOLS_SERVICE_NAME=producer`, `POWERTOOLS_METRICS_NAMESPACE=Producer`, `POWERTOOLS_LOG_LEVEL`, optional `MAX_FILE_BYTES`, `ALLOWED_CONTENT_TYPES`, `UPLOAD_URL_TTL_SECONDS` |
| `aws_sqs_queue` registrar + DLQ | SSE-SQS (`sqs_managed_sse_enabled = true`); main queue `visibility_timeout_seconds = 1800` (6× registrar timeout); `redrive_policy` maxReceiveCount 5; DLQ retention 14 d |
| `aws_sqs_queue_policy` | allow `s3.amazonaws.com` `sqs:SendMessage` with `aws:SourceArn = bucket ARN` and `aws:SourceAccount` |
| `aws_s3_bucket_notification` | `queue { events = ["s3:ObjectCreated:*"], filter_prefix = "raw/" }`, `depends_on` the queue policy |
| `aws_lambda_event_source_mapping` | batch 10, `maximum_batching_window_in_seconds = 5`, `function_response_types = ["ReportBatchItemFailures"]` |
| `aws_api_gateway_rest_api` | REGIONAL; resource `{proxy+}` with method `ANY` plus root `ANY`, both `authorization = "AWS_IAM"`; `AWS_PROXY` integration to the api Lambda |
| `aws_api_gateway_deployment` | `triggers = { redeploy = sha1(jsonencode([...resource/method/integration ids...])) }`, `create_before_destroy` |
| `aws_api_gateway_stage` "prod" | access logs to a log group (JSON with requestId, ip, caller, user, requestTime, httpMethod, resourcePath, status, responseLength); `xray_tracing_enabled = true` |
| `aws_api_gateway_method_settings` `*/*` | metrics on, logging INFO, throttling rate 50 / burst 100 (variables) |
| `aws_api_gateway_account` + role | CloudWatch logging role (account-level singleton; make it optional with `var.manage_apigw_account`) |
| `aws_lambda_permission` | `apigateway.amazonaws.com`, `source_arn = "${execution_arn}/*/*"` |
| Optional `aws_wafv2_web_acl_association` | when `var.waf_acl_arn` is set |
| `aws_iam_policy` "producer-api-invoke-${env}" | `execute-api:Invoke` on `${execution_arn}/prod/*/v1/*`, for callers |
| SNS + alarms | DLQ visible ≥ 1; registrar `Errors` ≥ 1 for 3×5 min; API `5XXError` ≥ 5 in 5 min; custom `Producer/Upload_rejected` ≥ 1 (missing data = not breaching); optional email subscription |

### 7.4 IAM (least privilege; write explicit `aws_iam_policy_document`s)
- **api role:** `dynamodb:GetItem/PutItem/UpdateItem/DeleteItem/Query` on the 3 tables and their indexes; `dynamodb:ConditionCheckItem` + transact actions on process and uploads; `s3:PutObject` on `raw/*` (presigned PUTs are signed by this role); `kms:GenerateDataKey`, `kms:Encrypt`, `kms:Decrypt` on the CMK; X-Ray put; logs via `AWSLambdaBasicExecutionRole`.
- **registrar role:** DynamoDB as above (no Delete needed); `s3:GetObject`, `s3:GetObjectVersion`, `s3:GetObjectAttributes` on `raw/*`; `kms:Decrypt`, `kms:GenerateDataKey`; `sqs:ReceiveMessage/DeleteMessage/GetQueueAttributes/ChangeMessageVisibility` on the queue; X-Ray; logs.
- No wildcards on resources except index ARNs (`${table_arn}/index/*`) and the `raw/*` prefix.

### 7.5 Variables and outputs
Variables: `env`, `region`, `table_prefix` (default `""`), `lambda_zip_path` / `lambda_s3_*`, `cors_origins` (list), `alarm_email` (optional), `max_file_bytes`, `allowed_content_types`, `api_throttle_rate`, `api_throttle_burst`, `log_retention_days`, `manage_apigw_account`, `waf_acl_arn` (optional), `tags`.
Outputs: `api_url` (stage invoke URL), `raw_bucket_name`, `process_table_name`, `articles_table_name`, **`articles_stream_arn`** (for the ingestion pipeline), `uploads_table_name`, `invoke_policy_arn`, `kms_key_arn`, `registrar_dlq_url`.

### 7.6 State and environments
- Backend: S3 bucket per account, key `producer/<env>/terraform.tfstate`, `encrypt = true`, `use_lockfile = true`.
- Default tags on the provider: `service=producer`, `env`, `owner`, `cost-center`.
- If any table already exists, add `import {}` blocks; never recreate a table that holds data.

## 8. Ingestion pipeline integration (document in README)
- EventBridge Pipes filter on the `articles` stream: `{"eventName":["INSERT","MODIFY"],"dynamodb":{"NewImage":{"status":{"S":["STAGED"]}}}}` and `{"eventName":["REMOVE"]}`.
- The file is always in S3 before the row exists, so the pipeline's Fetch step needs no "object not visible yet" retry.
- Pipeline process total = `registered_count` once `settled` is true.
- Reconciler: query `gsi_updated` for today and yesterday; compare `content_sha256`.

## 9. Observability
Powertools Logger (JSON, correlation id = API request id), Metrics (`Upload_registered|duplicate|superseded|rejected|ignored`), Tracer. Log keys: `process_id`, `upload_id`, `article_id`, `outcome`, `reason`.

## 10. CLI (`producer-cli`, in `client/`)
SigV4-signs API calls (`execute-api`) with the caller's AWS credentials; computes SHA-256; PUTs with the returned headers; parallel uploads (default 8).
Commands: `create-process`, `upload <pid> <files…>`, `ingest-dir <dir> --name --source [--close]`, `status <pid>`, `close <pid> [--expected-count]`. `--external-id path|filename|none` (default `path`, relative to the folder).

## 11. Tests

**Python (moto)** — keep or recreate these cases:
ticket presigned with checksum and no row before upload · register creates row and counts once · duplicate event → no change · SQS batch handler returns no failures · checksum mismatch → REJECTED, `rejected_count` +1, no row · re-upload in a new process → same article, `process_id` moves, `first_process_id` kept · out-of-order events → older upload SUPERSEDED · unknown/foreign objects ignored · close sets `expected_count`, blocks new tickets, double close → 409 · explicit expected count + late registrations → `settled` · no `external_id` → new article each time · disallowed content type → 422 · patch / delete / list · metadata kept when a re-upload omits it · API: full flow through the handler, 422/404 paths, path-traversal filename rejected.

**Terraform:** `fmt -check`, `validate`, `tflint`, `checkov` clean (justify any skip inline). `terraform test` plan assertions: articles stream view is NEW_AND_OLD_IMAGES; all tables have PITR + KMS; both API methods use `AWS_IAM`; bucket blocks public access and has TLS-only policy; the event source mapping reports batch item failures; prod has deletion protection.

**Post-deploy smoke test (dev):** `scripts/smoke.sh` runs `producer-cli ingest-dir tests/fixtures --source smoke --close`, then polls the process until `settled` and asserts `registered_count` equals the number of fixture files and one article row per file.

## 12. Phases and gates

**Phase 0 — Foundations**
- [ ] Repo, `uv`, CI (ruff, mypy, pytest, terraform fmt/validate/tflint/checkov)
- [ ] Remote state backend + provider pinning; `modules/kms`, `modules/tables`, `modules/storage`
- Gate: `terraform plan` clean for dev; tables, bucket and KMS reviewed

**Phase 1 — API**
- [ ] Python app (reuse reference code): models, repository, storage, service, api handler
- [ ] `build_lambda.sh`; `modules/lambda_fn`; `modules/api` with IAM auth, stage, logs, throttling, invoke policy
- Gate: deployed to dev; create process, get ticket, and close work through `producer-cli`

**Phase 2 — Registrar**
- [ ] `modules/registrar`: queue, DLQ, queue policy, notification, event source mapping
- [ ] Registrar handler + all idempotency rules in §6.2
- Gate: smoke test passes; duplicate and tampered-file scenarios verified in dev

**Phase 3 — Hardening**
- [ ] `modules/alarms`; optional WAF; `terraform test` suite; prod env with deletion protection and `prevent_destroy`
- [ ] README: API, data model, pipeline integration (§8), runbook (DLQ redrive, rejected uploads, stuck PENDING tickets)
- Gate: prod `plan` reviewed; checkov clean

## 13. Acceptance criteria
- [ ] Every requirement in §1 is demonstrably met by a test.
- [ ] `articles` rows appear only for verified files; tampered uploads are REJECTED with a reason.
- [ ] Same `external_id` from the same `source` always maps to the same `article_id`.
- [ ] Re-delivered S3 events and repeated uploads in one process never change `registered_count` beyond 1 per article.
- [ ] `terraform destroy` works in dev; prod tables and bucket are protected.
- [ ] Outputs include `articles_stream_arn` and `api_url`.

## 14. Rules of engagement
- Don't route file bytes through API Gateway or Lambda.
- Don't write `articles` rows from the API; only the registrar writes them (PATCH/DELETE excepted).
- No unbounded retries; unexpected errors go to the DLQ.
- Don't use `null_resource`/`local-exec` to build code during `plan`; build first, then plan.
- Ask before changing any contract in §4–§6; the ingestion pipeline depends on them.
- Questions to confirm before Phase 1: max file size (5 GB single PUT is the default; larger needs multipart), browser uploads (CORS origins), WAF required, whether the callers are IAM principals in the same account (else cross-account roles or a Cognito authorizer).