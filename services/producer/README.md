# Producer service

Registers ingestion processes, issues presigned S3 upload tickets, and records each verified file as a row in the DynamoDB `articles` table. The ingestion pipeline consumes that table's stream.

Infrastructure lives in [`/terraform`](../../terraform). The full brief is [`/claude.md`](../../claude.md).

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

## Build and deploy (dev)

```bash
./scripts/build_lambda.sh                       # -> dist/producer_lambda.zip (from uv.lock, linux/arm64)
make -C ../../terraform plan  ENV=dev STACK=producer
make -C ../../terraform apply ENV=dev STACK=producer
```

The zip is deterministic: the same `uv.lock` and source give the same `source_code_hash`, so Terraform updates the functions only when code changes. Environment settings live in `terraform/envs/<env>/producer.tfvars`. For CI, upload the zip to an artifacts bucket and set `lambda_s3_bucket`, `lambda_s3_key` and `lambda_s3_source_code_hash` there instead of `lambda_zip_path`.

## How a file becomes an article

1. `POST /v1/processes/{id}/uploads` returns a ticket: a presigned PUT URL with the checksum, content type and upload id signed in. The upload is recorded as `PENDING`.
2. The client PUTs the file straight to S3. S3 rejects a body whose SHA-256 differs from the declared one.
3. The S3 `ObjectCreated` event under `raw/` goes to SQS, and then to the registrar Lambda.
4. The registrar verifies the size and SHA-256 again, then writes the `articles` row (`STAGED`) and counts it in the process once. The `articles` stream carries it to the ingestion pipeline.

The registrar is idempotent. Duplicate events, retries and out-of-order deliveries can't create duplicate rows, double-count, or roll an article back to an older file. An older upload that arrives late becomes `SUPERSEDED`. Unexpected errors are retried, and after 5 receives the message goes to the dead-letter queue (`registrar_dlq_url` output).

## Smoke test (after deploying)

```bash
ENV=dev scripts/smoke.sh
```

The smoke test ingests `tests/fixtures` through the real API and S3, waits until the process is `settled`, and checks `registered_count`. It also checks that each file has exactly one `STAGED` article with the right SHA-256.

## CLI

`producer-cli` signs every API call with your AWS credentials (SigV4, `execute-api`). Your IAM principal needs the policy in the `invoke_policy_arn` output.

```bash
export PRODUCER_API_URL=$(cd ../../terraform/stacks/producer && TF_DATA_DIR=.terraform/dev terraform output -raw api_url)

uv run producer-cli create-process --name "Batch 1" --source news
uv run producer-cli upload prc_... docs/a.pdf docs/b.md            # external_id = path (default)
uv run producer-cli ingest-dir ./docs --name "Batch 2" --source news --close
uv run producer-cli status prc_...
uv run producer-cli close prc_... --expected-count 10
```

Run it outside the repo with `uv tool install './services/producer[cli]'`.

Options:
- `--external-id path|filename|none` controls re-sends. With `path` (the default), the external_id is the file's path relative to the folder, so re-uploading the same path updates the same article. With `none`, every upload creates a new article.
- `--concurrency` sets parallel uploads (default 8).
- `--profile` and `--region` choose the signing credentials.

`ingest-dir --close` closes the process with `expected_count` set to the number of files that reached S3.

## API

All routes are under `/v1` and need `AWS_IAM` auth. Errors are `404` (not found), `409` (process not OPEN) and `422` (`{message, errors: [{loc, msg}]}`). See brief §4 for the full contract.

| Method | Path |
|---|---|
| GET | `/v1/sources` |
| POST | `/v1/processes` |
| GET | `/v1/processes/{process_id}` |
| POST | `/v1/processes/{process_id}/close` |
| POST | `/v1/processes/{process_id}/uploads` |
| GET | `/v1/processes/{process_id}/uploads?limit=&next_token=` |
| GET | `/v1/processes/{process_id}/articles?limit=&next_token=` |
| GET | `/v1/uploads/{upload_id}` |
| GET / PATCH / DELETE | `/v1/articles/{article_id}` |

## Data model

These are the contract with the ingestion pipeline (brief §5). Table names carry the environment's `table_prefix` (`dev-` in dev, none in prod).

| Table | Key | Notes |
|---|---|---|
| `process` | `process_id` (`prc_<ULID>`) | `status` OPEN/CLOSED, `expected_count`, `registered_count`, `rejected_count`; the API adds the computed `settled` |
| `articles` | `article_id` | Written **only** by the registrar (PATCH/DELETE excepted). Stream: `NEW_AND_OLD_IMAGES`. GSIs `gsi_process` (`process_id`/`updated_at`, ALL) and `gsi_updated` (`updated_date`/`updated_at`, INCLUDE `content_sha256`, `process_id`, `upload_id`) |
| `producer_uploads` | `upload_id` (`upl_<ULID>`) | Tickets: PENDING → REGISTERING → REGISTERED / SUPERSEDED / REJECTED (+`reason`). TTL `expires_at_epoch`: PENDING 24 h, finished 30 days. Also holds `MEMBER#<process_id>#<article_id>` markers (`kind = membership`) that make counting exact. GSI `gsi_process_uploads` (`process_id`/`created_at`, INCLUDE) lists a process's uploads; it holds the markers too, which the API filters out by `kind` |

- **`article_id`**: `art_` + `uuid5(NAMESPACE, source \x1f external_id)` when `external_id` is given, so re-sending a document updates the same article. Without an `external_id`, it's `art_<ULID>`, a new article each time. `NAMESPACE` lives in `producer/config.py` and must never change, and a test pins it.
- **S3 objects**: `raw/{article_id}/{upload_id}/{filename}`. Objects are never overwritten, and the bucket is versioned and KMS-encrypted.
- **`settled`**: the status is CLOSED, no upload is still pending (each is registered, rejected, superseded or expired), and at least `expected_count` upload tickets were issued. Counters: `tickets_issued`, `pending_uploads`, `registered_count` (distinct articles), `rejected_count`, `superseded_count`, `expired_count`.

## Ingestion pipeline integration

The pipeline discovers the producer through SSM, so it doesn't need this stack's state:

```
/entingestion/<env>/producer/articles_stream_arn
/entingestion/<env>/producer/articles_table_name
/entingestion/<env>/producer/raw_bucket_name
/entingestion/<env>/producer/kms_key_arn
```

- **Stream source:** use an EventBridge Pipes filter on the `articles` stream, with these two patterns:
  ```json
  {"eventName": ["INSERT", "MODIFY"], "dynamodb": {"NewImage": {"status": {"S": ["STAGED"]}}}}
  {"eventName": ["REMOVE"]}
  ```
- **No visibility retry needed:** the file is always in S3, verified, before its row exists. The pipeline's fetch step doesn't need an "object not visible yet" retry. Read `s3_uri` together with `s3_version_id`.
- **Process totals:** once `GET /v1/processes/{id}` returns `settled: true`, the process total is `registered_count`.
- **Reconciler:** query `gsi_updated` for today's and yesterday's `updated_date`, and compare `content_sha256` with what was ingested.
- **Pipeline permissions:** it needs `kms:Decrypt` on the producer key to read the stream, the tables and the raw objects.

## Operations runbook

Alarms go to the `alarm_topic_arn` SNS topic (`alarm_emails` in the env tfvars). Registrar logs are structured JSON with `outcome`, `reason`, `process_id`, `upload_id`, `article_id` and `s3_key`:

```bash
aws logs tail /aws/lambda/producer-<env>-registrar --since 1h --filter-pattern '{ $.outcome = "rejected" }'
```

### DLQ not empty (`registrar-dlq-not-empty`)

A message failed 5 times. Registration is idempotent, so replaying is always safe.

1. Find the cause in the registrar logs: errors are logged with the exception, and the alarm `registrar-errors` usually fired too. Typical causes are throttling, missing IAM or KMS permissions, or a bug.
2. Fix the cause and deploy.
3. Move the messages back to the main queue:
   ```bash
   DLQ_ARN=$(aws sqs get-queue-attributes --queue-url "$(make -sC terraform output ENV=<env> | awk -F'"' '/registrar_dlq_url/{print $2}')" \
     --attribute-names QueueArn --query Attributes.QueueArn --output text)
   aws sqs start-message-move-task --source-arn "$DLQ_ARN"     # back to the registrar queue
   ```

### Rejected uploads (`uploads-rejected`)

`GET /v1/uploads/{upload_id}` shows `status: REJECTED` and a `reason`:

| Reason | Meaning | Action |
|---|---|---|
| `sha256_mismatch` | Bytes differ from the declared checksum (tampering, or written outside the presigned URL) | Investigate who wrote the key; client re-uploads with a new ticket |
| `size_mismatch: …` | Size differs from `size_bytes` | Client re-uploads with correct metadata |
| `too_large: …` | Over `max_file_bytes` | Raise the limit or split the file |
| `upload_id_metadata_mismatch` | `x-amz-meta-upload-id` doesn't match the key | Object wasn't written with the ticket; investigate |
| `object_not_found` | Object deleted before verification | Client re-uploads |

The rejected object stays in S3 for audit, and lifecycle rules handle it. Rejections count towards `settled`, so the process can still complete.

### Stuck PENDING uploads

- **Ticket issued but never used:** there's nothing to do. The row expires 24 h after creation (TTL).
- **Object is in S3 but the upload is still PENDING:** the event was lost or predates the notification. Confirm the object with `aws s3api head-object --bucket <raw_bucket> --key raw/<article_id>/<upload_id>/<filename>`, then replay its event:
  ```bash
  aws sqs send-message --queue-url <registrar_queue_url> --message-body '{"Records":[{"eventName":"ObjectCreated:Put","s3":{"bucket":{"name":"<raw_bucket>"},"object":{"key":"raw/<article_id>/<upload_id>/<url-encoded filename>"}}}]}'
  ```
- **Stuck in REGISTERING:** an attempt crashed. The message's next delivery, or a replay as above, finishes the registration.

### Processes that never settle

Check `pending_uploads` and `tickets_issued` against `expected_count`:
- `pending_uploads` above 0: uploads still without an outcome. A ticket whose file never arrives expires (the hourly janitor marks it EXPIRED after its URL expires, or re-sends the registrar an object whose event was lost); a REGISTERING upload is still in the registrar's queue (or its DLQ: redrive it).
- `tickets_issued` below `expected_count`: fewer uploads were requested than announced. Upload the rest, or close with the corrected `expected_count` (only while OPEN).
- An OPEN process nobody touches for `open_process_idle_days` (7) is closed by the janitor, expecting the tickets issued so far.

### Tear down a disposable environment

```bash
make -C terraform destroy ENV=dev STACK=producer DRY_RUN=1   # what would be deleted; changes nothing
make -C terraform destroy ENV=dev STACK=producer             # refuses prod; asks you to type the env name
```

The tables carry `prevent_destroy`, so the target removes them from Terraform state, destroys everything else, and then deletes the tables with the AWS CLI. The KMS key is scheduled for deletion, with a 30-day window. Destroy the ingestion stack first, or use `make -C terraform destroy-all ENV=dev`. The details, including how a failed run resumes, are in [docs/RUNBOOK.md](../../docs/RUNBOOK.md#tear-down-a-disposable-environment).
