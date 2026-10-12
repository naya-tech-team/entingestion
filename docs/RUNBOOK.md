# Ingestion runbook

How to deploy, watch and repair the ingestion pipeline. Diagrams of every flow are in [docs/diagrams](diagrams/); step-by-step flows in [WORKFLOWS.md](WORKFLOWS.md); requests for every route in the [Bruno collection](../bruno/README.md). The producer has its own runbook in [services/producer/README.md](../services/producer/README.md#operations-runbook). Design and decisions: [services/ingestion/README.md](../services/ingestion/README.md).

Every command assumes the repo root and AWS credentials for the account. Replace `<env>` with `dev` or `prod`.

## Where to look

| What | Where |
|---|---|
| Dashboard | CloudWatch → Dashboards → `ingestion-<env>` |
| Alarms and alerts | SNS topic `ingestion-<env>-alerts` (output `alert_topic_arn`): alarms (on ALARM only), and one hourly digest from the reconciler of the articles that failed permanently, by `error_code` |
| One article | `GET /v1/articles/{id}/status`: LATEST, the last 5 runs, the latest run's steps, and a console link to the execution |
| One process | `GET /v1/processes/{id}/summary`: indexed / skipped / failed counts, the producer's counts, failures by `error_code` |
| Logs | `/aws/lambda/ingestion-<env>-<function>`, JSON with `article_id`, `run_id`, `process_id`, `step`, `attempt` |
| Staged output | `s3://ingest-staging-<env>-<account>/<article_id>/v<doc_version>/` |

```bash
aws logs tail /aws/lambda/ingestion-<env>-index --since 1h --filter-pattern '{ $.article_id = "art_..." }'
```

## Credentials

Nobody uses access keys. Sign in with `aws login --profile entingestion` (MFA). Terraform runs as `entingestion-<env>-deploy` through the profile of the same name, and the Makefile refuses to run as anything else. `ALLOW_AMBIENT=1` is the break-glass switch. Setup, the guardrails and troubleshooting are in [AWS_ACCESS.md](AWS_ACCESS.md).

## Make commands

Every Terraform operation goes through [`terraform/Makefile`](../terraform/Makefile). Run it from the repo root with `make -C terraform <target> …`, or from `terraform/` with `make <target> …`.

**Deploy and inspect.** These need `ENV`, plus `STACK` (default `producer`). Each one first checks that you're running as `entingestion-<ENV>-deploy`.

| Command | Does |
|---|---|
| `make -C terraform whoami ENV=<env>` | Shows the identity Terraform would run as, and stops if it isn't `entingestion-<env>-deploy` |
| `make -C terraform init ENV=<env> STACK=<stack>` | `terraform init` with the env's backend (`envs/<env>/<stack>.s3.tfbackend`). Each env gets its own `.terraform/<env>` directory |
| `make -C terraform plan ENV=<env> STACK=<stack>` | init, then plan with `envs/<env>/<stack>.tfvars`. Saves the plan to `stacks/<stack>/<env>.tfplan` |
| `make -C terraform apply ENV=<env> STACK=<stack>` | Applies exactly the saved plan, then deletes it. Refuses if there's no saved plan |
| `make -C terraform output ENV=<env> STACK=<stack>` | Prints the stack's outputs. Add `-s` to drop make's own lines, for example in `$(…)` |
| `make -C terraform deploy ENV=<env> STACK=<stack>` | `plan`, asks you to type the env name, `apply` of that plan, then for `ingestion` `migrate`. `AUTO_APPROVE=1` skips the question (CI) |
| `make -C terraform migrate ENV=<env>` | Invokes `ingestion-<env>-migrate:live` and fails if the function did. Idempotent |
| `make -C terraform versions ENV=<env> FN=<function>` | The version the function's `live` alias points at, and the last 10 published versions |
| `make -C terraform rollback ENV=<env> FN=<function> VERSION=<n>` | Points the function's `live` alias at version `n`. See [Roll back a bad deploy](#roll-back-a-bad-deploy) |

**Tear down.** For disposable environments only; see [Tear down a disposable environment](#tear-down-a-disposable-environment).

| Command | Does |
|---|---|
| `make -C terraform destroy ENV=<env> STACK=<stack>` | Runs `scripts/teardown.py` for one stack: preflight, confirmation, Aurora, `terraform destroy`, vector tables. Re-run it to resume after a failure |
| `make -C terraform destroy-all ENV=<env>` | `destroy` for every stack that has tfvars in `envs/<env>/`, dependents first (ui, ingestion, then producer) |

**Upload UI.** See [UI.md](UI.md). Except `ui-build`, these need `ENV`, check the deploy role, and read the `ui` stack's outputs, so deploy that stack first.

| Command | Does |
|---|---|
| `make -C terraform ui-build` | `npm ci` and `npm run build` in `services/ui/web` (output: `dist/`). No AWS access |
| `make -C terraform ui-deploy ENV=<env>` | `ui-build`, then uploads `dist/` to the site bucket (hashed `assets/` immutable, everything else `no-cache`) and invalidates `/index.html` |
| `make -C terraform ui-password ENV=<env>` | Prompts for a username and password (not echoed) and stores only the SHA-256 of the `Authorization` header in the CloudFront KeyValueStore. Also how you rotate it |
| `make -C terraform ui-url ENV=<env>` | Prints the UI's URL |
| `make -C terraform ui-dev ENV=<env>` | Runs the web app on `http://localhost:5173`, proxying `/api` to that environment's CloudFront. Set `UI_USER` and `UI_PASSWORD` in your shell first |

**Static checks.** These need no AWS access; CI runs `check`.

| Command | Does |
|---|---|
| `make -C terraform check` | Runs all of the targets below except `clean` |
| `make -C terraform fmt` | `terraform fmt -check -recursive`. Run `terraform fmt -recursive` in `terraform/` to fix |
| `make -C terraform validate` | `terraform validate` on bootstrap, every stack and every module example |
| `make -C terraform test` | `terraform test` in bootstrap and every module and stack that has a `tests/` directory (plan-only) |
| `make -C terraform test-scripts` | ruff (lint and format check) and the unit tests for `terraform/scripts`, a syntax check of `ui_password.sh`, and the CloudFront Function tests (`node --test`) |
| `make -C terraform lint` | tflint, then checkov (`tests/` is skipped) |
| `make -C terraform clean` | Removes every `.terraform` working directory. The next `init` recreates them |

**Variables.**

| Variable | Default | Effect |
|---|---|---|
| `ENV` | none | `dev` or `prod`. Required by every deploy and teardown target |
| `STACK` | `producer` | `producer`, `ingestion` or `ui` |
| `AWS_PROFILE` | `entingestion-<ENV>` | The profile Terraform uses; see [AWS_ACCESS.md](AWS_ACCESS.md) |
| `AWS_EXPORT_CREDS=1` | off | The AWS CLI resolves the profile and passes plain environment credentials to Terraform. Use it if Terraform can't read an `aws login` session |
| `ALLOW_AMBIENT=1` | off | Break-glass: skips the role check and uses whatever credentials are active |
| `DRY_RUN=1` | off | `destroy` and `destroy-all`: list what would be removed, change nothing |
| `FINAL_SNAPSHOT=1` | off | `destroy` and `destroy-all`: take a final Aurora snapshot before deleting the cluster |
| `AUTO_APPROVE=1` | off | `deploy`: apply without asking |
| `FN`, `VERSION` | none | `rollback` and `versions`: the function (for example `ingestion-prod-embed`) and the version to go back to |

**Typical sequences:**

```bash
aws login --profile entingestion                                  # once per session
make -C terraform deploy ENV=dev STACK=producer                   # the producer first
make -C terraform deploy ENV=dev STACK=ingestion                  # then ingestion (+ migrate)
make -C terraform deploy ENV=dev STACK=ui                         # then the UI, then the producer again (first time; docs/UI.md)
make -C terraform ui-password ENV=dev && make -C terraform ui-deploy ENV=dev
make -C terraform plan  ENV=dev STACK=ingestion                   # or plan and apply separately
make -C terraform apply ENV=dev STACK=ingestion && make -C terraform migrate ENV=dev
make -C terraform destroy-all ENV=dev DRY_RUN=1                   # preview a teardown
make -C terraform check                                           # before every commit
```

The bootstrap stack (state bucket and key, deploy roles, workload boundaries, audit trail) has no make target. An admin applies it by hand with the `entingestion` profile; see [AWS_ACCESS.md](AWS_ACCESS.md#one-time-setup) and [terraform/bootstrap/README.md](../terraform/bootstrap/README.md). Apply it before the stacks whenever it changes: every role the stacks create carries the workload boundary it defines.

### State encryption (one time)

The backends (`envs/<env>/<stack>.s3.tfbackend`) set `kms_key_id` to the state key (`alias/entingestion-tfstate`), and the state bucket policy refuses any other encryption header. Objects written before that are SSE-S3. Re-encrypt each one in place once, as an admin (the deploy roles can't read the bootstrap state):

```bash
BUCKET=entingestion-tfstate-106751264162-us-west-2
KEY=arn:aws:kms:us-west-2:106751264162:alias/entingestion-tfstate
for k in dev/producer dev/ingestion prod/producer prod/ingestion bootstrap; do
  aws s3api head-object --bucket $BUCKET --key $k/terraform.tfstate >/dev/null 2>&1 || continue
  aws s3 cp s3://$BUCKET/$k/terraform.tfstate s3://$BUCKET/$k/terraform.tfstate \
    --sse aws:kms --sse-kms-key-id $KEY
done
aws s3api head-object --bucket $BUCKET --key dev/ingestion/terraform.tfstate --query ServerSideEncryption   # "aws:kms"
```

`make init` already runs `terraform init -reconfigure`, so the next `make plan` picks up the new backend settings. Older versions of each object stay SSE-S3 until the lifecycle rule expires them (90 days).

## Calling the control API

The API uses IAM auth. Attach the output `control_api_reader_policy_arn` (GET routes), `control_api_search_policy_arn` (`POST /v1/search` only) or `control_api_operator_policy_arn` (all routes) to your role, then sign requests with [awscurl](https://github.com/okigan/awscurl) (MIT):

```bash
API=$(make -sC terraform output ENV=<env> STACK=ingestion | awk -F'"' '/^control_api_url/{print $2}')
call() { uvx awscurl --service execute-api --region us-west-2 "$@"; }

call "$API/v1/tables"
call -X POST -d '{"article_ids": ["art_..."], "dry_run": true}' "$API/v1/ingest"
```

Every POST accepts `"dry_run": true`, which reports what would be queued without queuing it. Use it first.

| Route | Body | Does |
|---|---|---|
| `POST /v1/ingest` | `article_ids[]` (≤ 500), `force?`, `from_stage?`, `dry_run?` | Re-run articles. Without `force`, unchanged content is skipped |
| `POST /v1/processes/{id}/ingest` | `only_failed?`, `dry_run?` | Re-run a process's articles |
| `POST /v1/dlq/redrive` | `error_code?`, `max?` (≤ 1000), `dry_run?` | Re-run failed articles, resuming where staged output is intact. Without `error_code`, it also moves the ingest DLQ back |
| `POST /v1/reindex` | `filter{since?, process_id?, pipeline_version?}`, `tenant_id?`, `target_table?`, `from_stage` (`EMBED` or `INDEX`), `dry_run?` | Reindex into a vector table via the reindex state machine; with `tenant_id`, only that tenant's articles, and the table is added to that tenant's registry |
| `GET /v1/tables?tenant_id=` | | The live table behind `chunks_active`, row counts per table, the (tenant's) registry, every tenant registry, the profiles |
| `POST /v1/tables/active` | `table`, `tenant_id?`, `force?` | Make `table` live (default registry: also `chunks_active`; a tenant: its registry only), after the coverage and evaluation gates (`force` skips them). Also the rollback |
| `DELETE /v1/tables/writes/{table}?tenant_id=` | | Stop writing a table (retire it, or cancel a model change); rows stay |
| `POST /v1/search` | `tenant_id`, `query`, `k?` (≤ 50), `filters{source?, published_after?, process_id?}`, `rerank?` | Hybrid search of one tenant. The caller must read or write a source of that tenant in the producer's `source_access` |
| `GET /v1/articles/{id}/status` | | See "Where to look" |
| `GET /v1/processes/{id}/summary` | | See "Where to look" |

## Prerequisites for a new environment

Dev runs on the AWS free plan, which limits what it can test. Before the first prod apply:

1. **Account plan:** a paid plan. The free plan has no Aurora (only "express" clusters Terraform can't create), no RDS Proxy and no Textract.
2. **Lambda concurrency:** raise the account quota (Service Quotas → Lambda → Concurrent executions) from 10 to at least 1,000. Prod reserves 4 for Embed, and AWS keeps 100 unreserved. With 10, a reindex also starves the control API: API Gateway returns 500 without invoking the function.
3. **Bedrock model access** in the region: `amazon.titan-embed-text-v2:0` (embeddings), the model of every other `bedrock` embedding profile (for example `cohere.embed-multilingual-v3`), and `amazon.nova-lite-v1:0` (only for writing the synthetic golden set).
4. **Producer** deployed to the same env. Ingestion reads its SSM parameters under `/entingestion/<env>/producer/`.
5. **Alarm emails:** set `alarm_emails` in `terraform/envs/prod/ingestion.tfvars` and `producer.tfvars`. Each address confirms by email. **A prod plan fails without one** (variable validation: alarms nobody receives are worse than none). The prod tfvars still have `alarm_emails = []` on purpose while alerting is parked, so `make plan ENV=prod` stops there until an address is set.
6. **AWS Backup for DynamoDB:** prod has `backup_enabled = true`. Turn on AWS Backup's advanced features for DynamoDB once per account and region, or the cross-region copies of the tables fail: `aws backup update-region-settings --resource-type-management-preference DynamoDB=true` (and opt in to DynamoDB under `--resource-type-opt-in-preference`).
7. **Shared account:** dev and prod share this account, and so its Lambda concurrency and Bedrock quotas. Prod reserves concurrency for what must keep working when dev is busy (`dispatcher_reserved_concurrency`, `control_api_reserved_concurrency`, `index_reserved_concurrency`, `embed_reserved_concurrency`; the producer's `registrar_reserved_concurrency` and `api_reserved_concurrency`). Bedrock has no per-environment quota: a dev reindex or load test can fire prod's `bedrock-throttled` alarm.

## Deploy

```bash
services/ingestion/scripts/build_lambda.sh all                  # dist/core.zip, dist/docs.zip
make -C terraform deploy ENV=<env> STACK=ingestion               # plan, confirm, apply, migrate
```

Review the plan before you confirm: no data store is replaced or destroyed. `deploy` applies first and runs `migrate` right after. That order is safe because **migrations are expand-only**: they add tables, columns and indexes and never drop or rename what running code reads, so old code runs against the new schema. New code that reaches a table or column before its migration has run fails `E_DB_SCHEMA`, which is retried (the run ends FAILED_RETRYABLE and the reconciler re-queues it), so a late or failed migrate costs latency, not articles. To remove something, stop reading it in one release and drop it in a later one (expand, then contract). `make migrate ENV=<env>` repeats safely.

**Versions and aliases.** Every code or configuration change publishes a new version of each changed function, and the apply moves its `live` alias to it. The state machines, event source mappings, schedules, both APIs and the in-function calls (`query`, `tables`) all invoke `live`, so [a rollback](#roll-back-a-bad-deploy) is one alias update. The first apply with aliases (M5) publishes version 1 of every function, re-points the event source mappings in place (same UUID) and replaces the API Gateway invoke permissions; expect a few 5xx from the APIs during that apply.

Migration 004 creates `ingest_ops`, which the `tables` function uses for `GET /v1/tables` and `POST /v1/tables/active`; until it has run, those routes fail.

**Migrations 005 and 006** (tenants and row-level security; weighted `content_tsv`, `parent_text`, citation offsets) must run right after the apply that ships them: until then Index fails on the new columns (`E_DB_SCHEMA`: runs end FAILED_RETRYABLE and the reconciler re-queues them), and search, evaluation and the drift check see nothing for tenants without a setting. 006 rewrites both chunk tables (generated column) under a lock: run it in a quiet window, with ingestion paused (see [Pause and resume ingestion](#pause-and-resume-ingestion)) if the tables are large. The first update of each article afterwards may be a metadata refresh (no fingerprint stored yet), and every embedding is fetched once more from Bedrock (new cache key).

**Embedding profiles** (migration 007 and the profile tables). With the default `embedding_profiles` nothing changes: the first migrate records `chunks_titan2_1024` and `chunks_titan2_512` in `profile_tables` and creates no table (`"profile_tables_created": []`). A new profile's table is created by the migrate step of the deploy that adds it. Until then, deletes (which reach every profile's table) fail `E_DB_SCHEMA` and are retried, so run migrate right after the apply, as `deploy` does.

The zips are uploaded to the stack's artifacts bucket (they exceed Lambda's 50 MB direct upload). Each published version keeps its own copy of the code (counted against the regional 75 GB code storage); delete old versions now and then (`aws lambda delete-function --function-name <fn> --qualifier <n>`, never the one `live` points at).

**Smoke test** with a few generated articles (never the full 1,000 in dev):

```bash
cd services/ingestion
uv run python scripts/sample_corpus.py /tmp/smoke --count 3
uv run python scripts/gate_phase1.py --corpus /tmp/smoke --env <env> --timeout-minutes 15   # "GATE PASSED"
```

**Load test** (freshness p50/p95 and throughput from LATEST; costs Bedrock calls, keep it small on dev):

```bash
cd services/ingestion/scripts && uv run python load_test.py --env <env> --docs 10
```

## Roll back a bad deploy

Code: point the affected functions' `live` alias back at the previous version. It takes effect for new invocations at once; running executions continue on whatever their next step invokes.

```bash
make -C terraform versions ENV=<env> FN=ingestion-<env>-embed         # live: 7; versions 1..7
make -C terraform rollback ENV=<env> FN=ingestion-<env>-embed VERSION=6
```

A rollback holds until the next apply, which moves every changed function's alias to the new version again: revert the code (and rebuild) before deploying again. Infrastructure changes (IAM, alarms, the state machine definition) roll back by deploying the previous commit. Schema changes don't roll back; that's why migrations are expand-only. A bad vector table switch rolls back with `POST /v1/tables/active` (see [Change the embedding model](#change-the-embedding-model-or-rebuild)).

## Alarms

Every alarm is named `ingestion-<env>-alerts-<key>` and notifies the topic when it goes into ALARM (not on OK: `alarm_notify_on_ok` turns that on). Permanent failures aren't mailed one by one: MarkFailed counts them (`ArticlesFailedPermanent`, by `error_code`), and the reconciler sends **one digest per hour** when there are new ones: how many, by `error_code`, and up to 10 article ids. Escalations after 5 retries are in the same digest.

| Key | Means | Do |
|---|---|---|
| `ingest-dlq-new-messages` | New messages in the ingest DLQ (one alarm per 5 minutes with arrivals): bodies the dispatcher can't parse (sent there at once), or records still waiting after `queue_max_receive_count` (50) receives | Read one message (`reason` attribute for unparseable ones). Fix, then `POST /v1/dlq/redrive {}` moves the DLQ back |
| `backfill-dlq-new-messages` | The same for the backfill queue | `aws sqs start-message-move-task --source-arn <backfill DLQ arn>` |
| `pipe-dlq-new-messages` | The Pipe couldn't deliver stream records | Check the Pipe (IAM, KMS on the producer key). No replay needed: the hourly reconciler re-enqueues articles updated today or yesterday that have no matching LATEST. Then purge the pipe DLQ |
| `<queue>-dlq-not-drained` | A DLQ has held messages for 24 hours | Redrive or purge it |
| `ingest-queue-backlog` | The oldest ingest message is older than `queue_age_alarm_seconds` (15 min) | Usually every live slot is busy (the "Backlog" and `Dispatch_waiting_live` panels): it drains on its own, or raise `live_slots`. Otherwise the dispatcher is failing or throttled. Messages received more than 3 times drop out of this SQS metric |
| `runs-failing` | Executions failed (≥ `failed_runs_alarm_threshold` in 5 min) | See "Failed articles"; the next digest lists them by `error_code` |
| `runs-timing-out` | An execution ran longer than `execution_timeout_seconds` (3 h) | Find it in Step Functions; usually a stuck OCR job or Index lock wait. Its lease and slot expire and the reconciler resumes it |
| `step-<step>-slow` | That step's p95 stayed above `step_p95_alarm_ms` for 15 minutes | See "Slow steps" |
| `bedrock-throttled` | More than 50 Bedrock throttles in 5 minutes | Retries absorb bursts (throttled runs have their own retry budget). If sustained: fewer `live_slots`/`backfill_slots`, or a Bedrock quota increase. Dev load counts against the same quota |
| `freshness-lag` | **SLO:** p95 lag of stream runs above `freshness_alarm_seconds` (15 min) for 15 minutes | Look for a backlog (slots busy), throttling or slow Index first. Replays and reindexes are reported under their own `Trigger` and don't count |
| `backlog-age` | **SLO:** an article has been in a running status (CLAIMED..INDEXING, STAGED) for over `backlog_alarm_seconds` (30 min); published hourly by the reconciler | The "Backlog" panels show which status. Expired leases and STAGED articles are re-queued by the reconciler; a stuck step shows in its logs |
| `failure-ratio` | **SLO:** more than `failure_ratio_alarm_percent` (1%) of an hour's runs failed permanently (with at least 20 runs) | The digest has the `error_code`s; see "Failed articles" |
| `reconciler-errors` | The reconciler Lambda failed | Check its logs. Every reconciler step is idempotent, so the next hourly run repeats safely. Without it there's no digest and no backlog metric |
| `dispatcher-errors` | The dispatcher function itself failed for 15 minutes | Check its logs |
| `dispatcher-start-failing` | The dispatcher couldn't start executions for 15 minutes (`Dispatch_error`) | Its logs: permissions, the state machine, throttling. Waiting for a slot is not an error |
| `control-api-5xx` | ≥ 5 server errors in 5 minutes | Check the control-api logs. If there are no invocations, it is Lambda throttling (dev; prod reserves `control_api_reserved_concurrency`) |
| `aurora-cpu`, `aurora-capacity-near-max` | Aurora is busy, or near `aurora_max_acu` | Raise `aurora_max_acu`, or run less at once: lower `live_slots` / `backfill_slots` (that bounds running work; `dispatcher_max_concurrency` only bounds how fast runs start) |
| `aurora-buffer-cache` | Hit ratio below 90% | The working set (HNSW index) no longer fits in memory: raise `aurora_min_acu` |
| `proxy-connections` | RDS Proxy clients near `proxy_connections_alarm` | Too many concurrent in-VPC functions: set `index_reserved_concurrency`, or lower `live_slots` |
| `db-cpu`, `db-free-storage` | The single RDS instance (dev, `database_mode = "rds"`) is busy or full | Free storage: drop retired vector tables, or raise storage |

The producer's alarms (`producer-<env>-alarms-*`) include `registrar-dlq-new-messages`, `registrar-dlq-not-drained` and `registrar-queue-backlog` (oldest upload event over `registrar_queue_age_alarm_seconds`, 15 min: the registrar is failing, throttled or at `registrar_reserved_concurrency`).

**Escalation.** Parked until there is an on-call rotation: the first subscriber in `alarm_emails` owns the alarms. When one isn't acknowledged within an hour, the owner of the stacks (`mandatory_tags.owner`) takes it. Record the incident and the fix in the repository's issues.

### SLOs

| SLO | Target | Measured by | Alarm |
|---|---|---|---|
| Freshness | p95 < 15 min from producer update to searchable, stream runs | `FreshnessLagSeconds` (Finalize, dimension `Trigger`), p95 over 5 minutes | `freshness-lag`, 3 of 3 periods |
| Failures | < 1% of runs fail permanently | `Run_FAILED_PERMANENT / (Run_INDEXED + Run_FAILED_PERMANENT)` per hour | `failure-ratio` |
| Backlog age | No article in a running status for over 30 min | `BacklogOldestSeconds{status=RUNNING}` (reconciler, hourly; per status too, and `BacklogCount`) | `backlog-age` |

Work waiting for a slot sits on the queue, not in a status, so it shows as queue age (`ingest-queue-backlog`) and in freshness, not in `backlog-age`.

## Failed articles

A run ends in one of two failed states:
- **FAILED_RETRYABLE:** the reconciler retries it with backoff, up to 5 times, then escalates it. Throttling (Lambda concurrency, Bedrock) has its own budget of 20 (`throttle_count`, 5 to 40 minutes apart) and doesn't use up those 5.
- **FAILED_PERMANENT:** nothing retries it until you act.

`GET /v1/processes/{id}/summary` groups a process's failures by `error_code`, and `GET /v1/articles/{id}/status` shows the failing step and its message.

| Code | Class | Meaning | Fix |
|---|---|---|---|
| `E_THROTTLED`, `E_UPSTREAM_5XX`, `E_TIMEOUT` | transient | AWS or embedding-endpoint throttling, 5xx or a timeout | Retried automatically |
| `E_UPSTREAM_4XX` | permanent | An `openai_compatible` embedding endpoint refused the request (bad model name, API key or input) | Fix the profile or the secret, apply, then redrive `E_UPSTREAM_4XX` |
| `E_DB_CONN`, `E_DB_CONFLICT` | transient | Database connection or lock conflict | Retried automatically; if persistent, check the database panels |
| `E_DB_SCHEMA` | transient | A table or column the code needs doesn't exist: the code is ahead of the migrations | `make -C terraform migrate ENV=<env>`; the runs recover on their next retry |
| `E_LEASE_BUSY` | transient | Two claims raced for one article in the same instant | Retried. (A delete that finds a run going no longer fails: it marks the article, and the run hands over.) |
| `E_SOURCE_MISSING` | permanent | The producer object is gone | Re-upload through the producer |
| `E_SOURCE_CHANGED` | permanent | The object's checksum no longer matches the producer row | Re-upload through the producer |
| `E_UNREADABLE` | permanent | Corrupt or encrypted file | Fix the file and re-upload, or accept it |
| `E_EMPTY_TEXT` | skip | No text found, for example a scanned PDF with OCR off. The article ends SKIPPED, not failed | Turn OCR on (see "OCR switch") and re-run with `force`, or accept it |
| `E_TOO_LARGE` | permanent | Over 50 MB (with OCR on, PDFs go to Textract instead, up to 500 MB), or more chunks than `max_chunks_per_article` (5,000) | Turn OCR on, split the file, or raise `max_chunks_per_article` and redrive |
| `E_VALIDATION_<CHECK>` | permanent | The Validate gate rejected the chunks or vectors | A bug or a bad document; read the step's `error_msg` |
| `E_BUG` | permanent | An unexpected exception | Fix the code, deploy, then redrive |

After the fix, redrive. Each article resumes after its last successful step whose staged output is still intact, so Bedrock isn't called again for work already done:

```bash
call -X POST -d '{"error_code": "E_BUG", "dry_run": true}' "$API/v1/dlq/redrive"   # how many
call -X POST -d '{"error_code": "E_BUG"}' "$API/v1/dlq/redrive"
```

A process's counters follow: `failed −1, indexed +1` per recovered article, and the summary moves from COMPLETE_WITH_ERRORS to COMPLETE.

To re-run one article from scratch, ignoring the unchanged-content check: `POST /v1/ingest {"article_ids": ["art_..."], "force": true}`. Use `"from_stage": "CHUNK"` (or any step) to redo only the rest.

## Stuck articles

The reconciler runs hourly and handles these on its own:
- expired leases (a crashed step) resume where the staged output is intact;
- STAGED for over 30 minutes re-runs from Index;
- producer articles with no matching LATEST are enqueued;
- open process summaries get their completion check.

To act sooner, invoke it by hand:

```bash
aws lambda invoke --function-name ingestion-<env>-reconciler --payload '{}' /dev/stdout
```

Weekly, it also runs a drift check (`{"drift": true}`). That check compares active chunks per article in pgvector with `LATEST.chunk_count`, rebuilds mismatches from staging, and removes rows of deleted articles. The dashboard's "Reconciler" panel shows what it did.

**Admission slots.** At most `live_slots` runs from the ingest queue (8 by default, 2 x `embed_reserved_concurrency` when that is set) and `backfill_slots` (2) from the backfill queue are in flight; the rest wait on their queue. A run holds its slot (`#SLOTS/<lane>#<i>` in `ingestion_status`) until it ends, and extends it at every step. A run that dies without ending (or one the state machine ends as stale) frees its slot when the slot's lease runs out (`lease_seconds`, 35 min). To see who holds what:

```bash
aws dynamodb query --table-name <prefix>ingestion_status --key-condition-expression 'pk = :p' \
  --expression-attribute-values '{":p": {"S": "#SLOTS"}}' --query 'Items[].[sk.S, run_id.S, expires_at.N]' --output text
```

Freeing a slot by hand is never needed for correctness; deleting its item frees it at once. Changing `live_slots` or `backfill_slots` takes effect with the next dispatcher invocation after the apply; no items need seeding.

## Pause and resume ingestion

Stop new runs (for a migration, an incident, or Aurora maintenance); running executions finish:

```bash
aws pipes stop-pipe --name ingestion-<env>-articles                  # stream records stay in the DynamoDB stream (24 h)
for uuid in $(aws lambda list-event-source-mappings --function-name ingestion-<env>-dispatcher:live \
    --query 'EventSourceMappings[].UUID' --output text); do
  aws lambda update-event-source-mapping --uuid "$uuid" --no-enabled  # ingest + backfill queues hold their messages
done
```

The reconciler may keep running: it only adds messages to the paused queues. Resume with `aws pipes start-pipe --name ingestion-<env>-articles` and `--enabled` on both mappings. Within 24 hours (the stream's retention) the Pipe picks up where it stopped. After that, the hourly reconciler still re-enqueues anything the producer updated today or yesterday without a matching LATEST; for a longer pause, re-run the processes registered meanwhile (`POST /v1/processes/{id}/ingest`). Queued messages are kept 4 days. A `terraform apply` re-enables the mappings and starts the Pipe, so resume before deploying.

## Cancel a reindex

```bash
aws stepfunctions list-executions --state-machine-arn <reindex state machine arn> --status-filter RUNNING
aws stepfunctions stop-execution --execution-arn <arn>               # stops queueing new batches
aws sqs purge-queue --queue-url <backfill queue url>                 # drops batches already queued
call -X DELETE "$API/v1/tables/writes/<table>"                       # live runs stop writing the new table
                                                                     # (add ?tenant_id=<t> for a tenant's reindex)
```

Shadow runs already started finish their article (or end at their next step). The new table keeps what it has; reindex again later, or leave it unused. The live table is never touched.


## Change the embedding model, or rebuild

Each embedding profile has its own table (`embedding_profiles` in the tfvars; `migrate` creates the tables: [Add a model](#add-a-model)). `chunks_active` is a view over the default registry's live table, so switching and rolling back are instant. These steps move the default registry (every tenant without its own); to move one tenant, see [Move a tenant to another model](#move-a-tenant-to-another-model).

1. **Reindex** into the new table. `EMBED` re-embeds from the staged chunks; Extract and Chunk don't run again:
   ```bash
   call -X POST -d '{"target_table": "chunks_titan2_512", "from_stage": "EMBED", "dry_run": true}' "$API/v1/reindex"
   call -X POST -d '{"target_table": "chunks_titan2_512", "from_stage": "EMBED"}' "$API/v1/reindex"
   ```
   The response links the reindex execution. Watch the backfill queue drain on the dashboard. On dev, the 10-concurrency limit makes this slow and can make the control API return 500 meanwhile.
2. **Evaluate** before switching, with `--baseline` so the live table is scored on the same golden set. `--min-recall10` exits 1 below the threshold:
   ```bash
   cd services/ingestion
   uv run python scripts/evaluate.py --env <env> --table chunks_titan2_512 --baseline --min-recall10 0.9
   ```
   Scores are chunk-level (recall@5/@10, nDCG@10, MRR, with 95% CIs) plus article recall@10. The article-level baseline on dev (2026-10-11, 46 synthetic questions over 23 articles): `chunks_titan2_1024` recall@5 1.00, recall@10 1.00, MRR 0.877; `chunks_titan2_512` 1.00 / 1.00 / 0.888. Each report goes to `_eval/<table>/` in staging, with the questions that missed.
3. **Switch:** `call -X POST -d '{"table": "chunks_titan2_512"}' "$API/v1/tables/active"`. The **evaluation gate** refuses (409, `"gate": "evaluation"`) unless the table's newest report is under `eval_max_age_days` (7) old and its recall@10 is at most `eval_tolerance` (0.02) below the live table's on the same golden set. The 409 says which; re-run step 2.
4. **Roll back:** the same call with the old table, usually with `"force": true` (its report is likely older than 7 days). Nothing is deleted, so you can switch back and forth.

**Rebuild a table from staging** (lost or corrupt rows) uses `"from_stage": "INDEX"`. It reloads the staged vectors without calling Bedrock, so even a full rebuild is cheap.

**Truncate.** The rebuild test (UC10) first empties a table with `migrate` (`{"action": "truncate", "table": "<t>", "confirm": "<t>"}`). `allow_truncate` in the env tfvars (`ALLOW_TRUNCATE` on the function) switches this off; it is `false` in prod, so a prod rebuild reindexes over the existing rows instead.

The table status and switch run in the `tables` function as the database user `ingest_ops`, never with the master credentials. `ingest_ops` can read the vector tables and replace the `chunks_active` view (owned by `ingest_views`), but can't write rows.

**Golden set.** `scripts/make_golden_set.py --env <env>` writes a new set of synthetic questions to `_eval/golden/<YYYYMMDD-HHMM>.jsonl` in staging (never overwriting one) and points `_eval/golden/LATEST` at it, using Nova Lite on chunks sampled across each document, plus `--no-answer` questions. Each line is `{"id", "question", "article_id", "doc_version", "chunk_ids", "no_answer"}`. Replace it with real user questions when you have them: a new file in the same format, then update `LATEST`. Questions whose article was deleted or re-versioned are dropped by the eval (and counted in the report): make a new set after large content changes. The old `_eval/golden.jsonl` (article ids only) is still read when there's no `LATEST`.

## Add a model

A model is an embedding profile (docs/ARCHITECTURE.md "Embedding models"); its table is `chunks_<name>`.

1. **Model access.** For a Bedrock model, enable it in the Bedrock console (Model access) in the region. For an `openai_compatible` endpoint, store its API key in Secrets Manager (a plain string, or JSON `{"api_key": "..."}`); the endpoint must be reachable from the internet over HTTPS.
2. **Add the profile** to `embedding_profiles` in `terraform/envs/<env>/ingestion.tfvars`, keeping the existing ones (the default block is in the comments there), e.g.
   ```hcl
   cohere_m3 = { provider = "bedrock", model = "cohere.embed-multilingual-v3", dimension = 1024, max_input_tokens = 512, batch_size = 96 }
   ```
   Check the model's input limit: Validate fails chunks above `max_input_tokens` (Cohere v3: 512) rather than letting the model truncate them. Chunking is shared by every table, so lower `CHUNK_TARGET_TOKENS` or turn on parent-child chunking first if needed.
3. **Apply and migrate:** `make -C terraform deploy ENV=<env> STACK=ingestion`. The migrate step creates the table (`"profile_tables_created": ["chunks_cohere_m3"]`); it refuses (fails) if a table of that name exists with another dimension. `GET /v1/tables` lists the profile and the empty table.
4. **Fill, evaluate, switch, retire** as in [Change the embedding model](#change-the-embedding-model-or-rebuild) (for everyone) or [Move a tenant to another model](#move-a-tenant-to-another-model) (for one tenant).

Remove a profile only after no registry writes or reads its table (`GET /v1/tables`): its table and rows stay in the database; deletes no longer reach them.

## Move a tenant to another model

One tenant moves; every other tenant, and `chunks_active`, stay where they are. Example: `t-news` to `chunks_cohere_m3` (the profile exists: [Add a model](#add-a-model)).

1. **Reindex the tenant** into the new table. This also gives the tenant its own registry (a copy of the default one plus the new table), so its live runs write both tables from now on:
   ```bash
   call -X POST -d '{"tenant_id": "t-news", "target_table": "chunks_cohere_m3", "from_stage": "EMBED", "dry_run": true}' "$API/v1/reindex"
   call -X POST -d '{"tenant_id": "t-news", "target_table": "chunks_cohere_m3", "from_stage": "EMBED"}' "$API/v1/reindex"
   call "$API/v1/tables?tenant_id=t-news"     # registry: live chunks_titan2_1024, writes [.., chunks_cohere_m3]
   ```
2. **Evaluate** the new table against the live one (reports are per table, over every tenant's golden questions):
   `uv run python scripts/evaluate.py --env <env> --table chunks_cohere_m3 --baseline`.
3. **Switch** the tenant: `call -X POST -d '{"tenant_id": "t-news", "table": "chunks_cohere_m3"}' "$API/v1/tables/active"`. The coverage gate counts only `t-news`'s indexed articles (409 with a sample if some are missing: reindex with `"filter": {"only_uncovered": true}`). Search for `t-news` now embeds queries with Cohere and reads `chunks_cohere_m3`; `chunks_active` doesn't change.
4. **Roll back** if needed: the same call with the old table (`"force": true` if its report is old). The old table is still written, so it is exact.
5. **Retire** the old table for the tenant once satisfied: `call -X DELETE "$API/v1/tables/writes/chunks_titan2_1024?tenant_id=t-news"`. Its rows stay (they belong to `t-news` and are only read by `t-news`'s searches, which no longer read that table); the drift check leaves them alone.

A **new** tenant can start on a profile without a reindex: `tenant_profiles = { "t-new" = "cohere_m3" }` in the tfvars, then apply. Don't map a tenant that already has articles this way: its searches would switch at once to a table without them.

If an article's producer row moves to a tenant whose registry writes other tables, its next run re-embeds it into those tables, and its rows in the old tables are re-tagged to the new tenant.

## Retrieval switches

All off by default (paid, or unavailable on the free plan). Set them in the env tfvars and apply:
- **`rerank_enabled`:** `POST /v1/search` with `"rerank": true` reranks with Bedrock `rerank_model_id` (`amazon.rerank-v1:0`; enable model access first). Billed per query. If reranking fails, results come back in the fused order with `"reranked": false` and the `RerankFailed` metric.
- **`pii_screening`:** Normalize screens every new version with Comprehend `DetectPiiEntities` (`pii_min_score`, 0.8) and applies each source's `pii` in the producer's `source_access` (`block`, `redact`, `allow`; default `allow`), so set those in the producer tfvars first. Blocked articles end SKIPPED with `E_PII_BLOCKED`; LATEST shows `pii_count`, `pii_types`, `pii_action`. Articles indexed before it was on aren't screened until they're re-run (`POST /v1/reindex` from `EMBED` doesn't re-normalize: use `POST /v1/ingest` with `force`).
- **`parent_child_enabled`:** smaller chunks matched, section text returned as `context`. It changes every chunk: bump `pipeline_version` (every article re-runs on its next update) or force a re-run.

Searching needs the producer's `source_access` to list the searching role as a reader (or writer) of the tenant's sources; the control API re-reads it at most every 5 minutes.

## OCR switch

Scanned PDFs, and PDFs over 50 MB, go to Textract through the state machine (OcrStart → poll → OcrCollect). `ocr_enabled` in the env tfvars turns this on or off:
- **Off** (dev, free plan): scanned PDFs end SKIPPED, and PDFs over 50 MB fail with `E_TOO_LARGE`.
- **On:** after turning it on, re-run the skipped articles with `POST /v1/ingest {"article_ids": [...], "force": true}`.

OCR with real Textract hasn't run yet, because the free plan blocks it. Run acceptance test UC12 in the first paid environment:

```bash
cd services/ingestion && uv run python scripts/acceptance_phase3.py uc12 --env <env>
```

## Backups and restore

| Store | Protection | Restore |
|---|---|---|
| Status and cache tables (DynamoDB) | PITR (35 days), KMS; `prevent_destroy`; deletion protection in prod. Prod (`backup_enabled`): AWS Backup daily, 35 days, copied to `backup_copy_region` | PITR or a recovery point, restored **to a new table**; see below |
| Producer tables (process, articles, uploads) | The same, in the producer stack | The same |
| Staging bucket (S3) | Versioned, KMS; prod can't be force-destroyed; old versions go to Glacier IR after 30 days | Restore an object version. Staging is the source for every rebuild |
| Producer raw bucket (S3) | Versioned, KMS | Restore an object version |
| Vectors, prod (Aurora) | Two instances (`aurora_instance_count`: a reader in another AZ that Aurora promotes on failover), automated backups (7 days) with PITR, a final snapshot on delete, deletion protection, `prevent_destroy`; AWS Backup daily, copied to `backup_copy_region` | Restore the cluster to a point in time, or simpler: rebuild the tables from staging (`from_stage: INDEX`) |
| Vectors, dev (RDS instance) | 1-day backups (free-plan maximum); no AWS Backup | Rebuild from staging |

**RPO and RTO (prod).**

| Failure | RPO (data lost) | RTO (back in service) |
|---|---|---|
| An AZ | 0 (Multi-AZ tables, Aurora reader promoted) | Minutes (Aurora failover) |
| A bad write or deletion in a table | Seconds (PITR) | About 1 hour: restore to a new table, copy back (below) |
| Vector rows lost or corrupt | 0 (rebuilt from staging) | Rebuild time: about the article count / throughput of `backfill_slots` runs |
| The Aurora cluster | 5 minutes (PITR) | 1 to 2 hours (restore, point the stack at it) |
| The region | 24 hours (daily copies in `backup_copy_region`) | A day or more: no standby stack in the other region; restore the copies there and deploy the stacks with that region (not yet rehearsed) |

S3 buckets aren't replicated to another region yet (a regional loss loses objects written since the last copy, and staging has no copy at all). **Follow-up:** S3 Cross-Region Replication of the raw and staging buckets (both are versioned already) to buckets in `backup_copy_region`, behind a prod-only variable; or AWS Backup for S3. Until then staging can be regenerated from raw by re-running, but raw has no second copy.

### Restore a DynamoDB table

Tables have fixed names (`<table_prefix>ingestion_status`, ...) that the stack and every function use, and a restore can only create a new table. So restore beside the original, then copy back:

1. **Pause ingestion** (see [Pause and resume ingestion](#pause-and-resume-ingestion)) so nothing writes meanwhile.
2. **Restore to a new name** with the stack's key:
   ```bash
   aws dynamodb restore-table-to-point-in-time --source-table-name prod-ingestion_status \
     --target-table-name prod-ingestion_status-restore-20261011 --restore-date-time 2026-10-11T08:00:00Z \
     --sse-specification-override Enabled=true,SSEType=KMS,KMSMasterKeyId=<stack key arn>
   ```
   (or `aws backup start-restore-job` from a recovery point in the vault `ingestion-<env>-backup`, or its copy in the other region). Wait until it's ACTIVE (GSIs included).
3. **Copy the items back** over the live table: an export of the restored table to S3 (`aws dynamodb export-table-to-point-in-time`) and a small script that `BatchWriteItem`s every item into the live table, or, for a few items, `get-item` / `put-item` by hand. Items written after the restore point that you want to keep must be merged first (compare `updated_at`). Don't point the stack at the restored table: its name is outside `table_prefix`, IAM and alarms.
4. **Delete the restored table**, resume ingestion, and run the reconciler once (`{"drift": true}` after a status-table restore) so the vector tables and LATEST agree again.

For a lost table (deleted despite protection): restore under the original name instead (the name is free), then `terraform plan` must show no change to it.

### Restore drill (prod, every quarter)

1. Restore `ingestion_status` and `articles` to a point one hour ago under new names (step 2 above), and the Aurora cluster to a new cluster from its newest recovery point.
2. Check: item counts within the hour's writes of the live tables; `SELECT count(*) FROM chunks_active` on the restored cluster close to the live one.
3. Time it: that's the measured RTO. Note it here with the date.
4. Delete the restored tables and cluster.

## Upload UI

The hosted upload and tracking page; design, security model and first-time setup in [UI.md](UI.md).

- **Deploy a new web build:** `make -C terraform ui-deploy ENV=<env>`. It needs no apply. A BFF code change goes through `make deploy ENV=<env> STACK=ui` after `services/ui/bff/scripts/build_lambda.sh`, and rolls back like any function (`FN=ui-<env>-bff`).
- **Rotate the login:** `make -C terraform ui-password ENV=<env>`, then tell the users. Everyone's browser asks again on the next request. It needs no apply.
- **Everyone gets a 401:** no login is set yet (a new KeyValueStore is empty), or it was rotated. Run `ui-password`.
- **The page loads but every `/api/*` call fails:**
  - **403 from CloudFront:** check the function URL permissions. The stack grants both `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` to this distribution only.
  - **A `forbidden` body:** the request failed the BFF's CSRF guard. It needs `X-Requested-With: entingestion-ui` and a same-origin `Sec-Fetch-Site`.
  - **502:** the BFF couldn't reach an API. Read the `ui-<env>-bff` log group.
  - **The producer's 403 "not allowed":** the BFF role isn't a writer of that source. Add `arn:aws:iam::<account>:role/ui-<env>-bff` to its `writers` in `envs/<env>/producer.tfvars`.
- **Uploads fail with a CORS error in the browser console:** the raw bucket's CORS doesn't list the UI origin yet. Apply the producer stack again after the ui stack (it reads `/entingestion/<env>/ui/origin`).

## Tear down a disposable environment

```bash
make -C terraform destroy-all ENV=dev DRY_RUN=1         # preflight + what would be deleted; changes nothing
make -C terraform destroy-all ENV=dev                   # ui, ingestion, then producer; confirm each by typing the env name
make -C terraform destroy ENV=dev STACK=ingestion       # one stack only
```

`destroy-all` takes the stacks in reverse dependency order: ui first, because it calls both APIs and uses the producer's key; then ingestion, because it reads the producer's stream, bucket and key. Each stack goes through [`terraform/scripts/teardown.py`](../terraform/scripts/teardown.py), because `terraform destroy` alone can't remove the data stores:

1. **Preflight, changing nothing.** It refuses prod. It also stops if any data store still has a prod setting: deletion protection on a table, cluster or instance, or `force_destroy` off on a bucket. These would make the destroy fail half way.
2. **Confirm.** It lists the Aurora clusters and tables it will delete, and you type the env name. Without a terminal (CI, a pipe) it never proceeds.
3. **Aurora** (`database_mode = "aurora"`). It deletes the cluster's instances, then the cluster, and waits. The cluster carries `prevent_destroy`, and its subnet, security and parameter groups can't be deleted while it exists. Add `FINAL_SNAPSHOT=1` to keep a snapshot (`<cluster>-teardown-<time>`).
4. **Terraform.** It removes the protected resources from state and runs `terraform destroy`. With functions in the VPC this can take 20–40 minutes: AWS releases Lambda network interfaces slowly, and Terraform waits for them before deleting the security groups and subnets.
5. **Tables.** It deletes the DynamoDB tables (they carry `prevent_destroy`) with the AWS CLI and waits.

**Resume.** If a step fails (a timeout, a throttle, an expired session), fix the cause and rerun the same command. Before anything leaves the state, the script records the protected resources in `stacks/<stack>/.terraform/<env>/teardown-pending.json`. A rerun therefore still deletes them, and skips whatever is already gone.

**What remains afterwards, by design:**
- the KMS keys, pending deletion for their window (30 days);
- the state files in the bootstrap state bucket, now empty;
- the bootstrap stack itself, which is shared by every environment in the account;
- a final Aurora snapshot, if you asked for one.

Nothing outside each stack's state is touched. That matters because the account also runs unrelated workloads.

The script is tested with stub `terraform` and `aws` CLIs (`make test-scripts`, part of `make check`). The tests cover:
- call order;
- every preflight block;
- the confirmation;
- resuming after a failed destroy.

The rds-mode path (dev) has been preflighted against the live dev stacks; the Aurora path only against the stubs, because the free plan can't create Aurora.
