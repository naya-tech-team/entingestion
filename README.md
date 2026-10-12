# entingestion

The article ingestion platform on AWS. It has three parts:

- **Producer** (`services/producer`), built now. It registers processes, issues presigned S3 upload tickets, verifies each uploaded file, and writes it to the DynamoDB `articles` table.
- **Ingestion pipeline** (`services/ingestion`). It consumes the `articles` stream and indexes chunks in Aurora PostgreSQL + pgvector. Brief: [`INGESTION_HANDOFF_PROMPT.md`](INGESTION_HANDOFF_PROMPT.md). Documentation: [`docs/`](docs/README.md): getting started, architecture, workflows, use cases, API, runbook, and draw.io diagrams. API requests: [`bruno/`](bruno/README.md).

- **Upload UI** (`services/ui`). A hosted page for uploading a batch of files and following each one to `INDEXED`, behind one shared login: [`docs/UI.md`](docs/UI.md).

The producer's design brief is [`claude.md`](claude.md).

## Layout

```
services/<service>/              application code (Python, uv), tests, build scripts
docs/                            getting started, architecture, workflows, use cases, API, runbook, diagrams
bruno/                           Bruno API collection (producer + control API, runnable use cases)
terraform/
  Makefile                       make plan|apply ENV=<env> STACK=<stack>; make check
  bootstrap/                     remote state bucket + KMS key, once per account (local state)
  scripts/teardown.py            make destroy / destroy-all for disposable envs (data stores, Aurora, resume)
  modules/<name>/                generic, reusable building blocks, each with tests/ and examples/basic
  stacks/<stack>/                deployable root per service: composes modules, with tests/
  envs/<env>/<stack>.tfvars          everything that differs between environments
  envs/<env>/<stack>.s3.tfbackend    where that environment's state lives
```

Each stack's code exists once. Environments differ only in their tfvars, so dev and prod can't drift apart in code. The Makefile always pairs an environment's backend with its tfvars and keeps a separate `.terraform/<env>` directory for each environment. You can't plan prod variables against dev state by accident.

| Generic module | Purpose |
|---|---|
| `kms_key` | Customer-managed key with rotation and an alias; extra policy statements for service principals |
| `s3_bucket` | Private bucket: public access block, BucketOwnerEnforced, SSE-KMS, TLS-only policy; lifecycle and CORS through inputs |
| `dynamodb_table` | On-demand table: PITR, CMK encryption and `prevent_destroy`; GSIs, stream and TTL through inputs |
| `lambda_function` | Function, role scoped to its own log group, pre-created encrypted log group, X-Ray; zip (local/S3) or container image; optional VPC |
| `lambda_sqs_trigger` | SQS -> Lambda mapping with partial batch responses always on and optional max concurrency |
| `vpc` | Private-only VPC (no IGW/NAT), subnet + route table per AZ, flow logs, locked default SG |
| `security_group` | Deny-by-default group with labelled ingress/egress rules |
| `vpc_endpoints` | Free S3/DynamoDB gateway endpoints (+ prefix lists) and opt-in interface endpoints |
| `aurora_postgres` | Aurora PostgreSQL Serverless v2: managed master secret, IAM auth, force_ssl, logging, PI, `prevent_destroy` |
| `rds_proxy` | RDS Proxy with end-to-end IAM auth (no stored app passwords), TLS required |
| `rds_postgres` | Single RDS for PostgreSQL instance: private, managed master secret, IAM auth, force_ssl (fits the AWS free plan) |
| `eventbridge_pipe` | DynamoDB stream -> SQS Pipe with filters, bisect + DLQ, least-privilege role |
| `step_function` | Standard state machine: ALL-event logs (no data), X-Ray, CMK encryption |
| `rest_api_lambda_proxy` | Regional REST API, root + `{proxy+}` with `AWS_IAM`, stage logs, X-Ray, throttling, invoke policy |
| `sqs_queue` | Queue + DLQ (redrive, SSE-SQS, redrive-allow policy); extra resource policy through inputs |
| `waf_web_acl` | Regional WAF: managed rule groups (per-rule COUNT overrides), per-IP rate limit, associations, redacted logs |
| `alarms` | KMS-encrypted SNS topic (account-scoped publish), email subscriptions, metric alarms defined as data |
| `scheduler_schedule` | EventBridge Scheduler schedule with its own least-privilege role, retries and an optional DLQ |

### Module versions

Each module is released with a git tag `modules/<name>/vX.Y.Z` (semantic versioning: a breaking interface change is a new major). The stacks in this repo use relative paths. A caller outside the repo pins a tag:

```hcl
module "queue" {
  source = "git::<repo url>//terraform/modules/sqs_queue?ref=modules/sqs_queue/v1.0.0"
}
```

```bash
git tag -l 'modules/*'                                   # released versions
git tag -a modules/<name>/v1.1.0 -m "<what changed>"     # after merging a module change
```

Every module is at `v1.0.0` (ingestion Phase 4). Push the tags along with `main` once the repo has a remote.

State is kept per environment and per stack at `s3://entingestion-tfstate-<account>-<region>/<env>/<service>/terraform.tfstate`, with S3 native locking.

## Cross-service hand-off

Each service publishes the values other services need to SSM Parameter Store under `/entingestion/<env>/<service>/`. For example, the producer publishes `articles_stream_arn`, `articles_table_name`, `process_table_name`, `raw_bucket_name` and `kms_key_arn`. Consumers read these with `data "aws_ssm_parameter"` instead of reading another stack's state.

## First-time setup (per account)

No access keys: sign in with MFA through `aws login`, and Terraform runs as per-environment deploy roles ([docs/AWS_ACCESS.md](docs/AWS_ACCESS.md)).

```bash
aws login --profile entingestion
cd terraform/bootstrap && AWS_PROFILE=entingestion terraform init && AWS_PROFILE=entingestion terraform apply   # state bucket + deploy roles
cd .. && make plan ENV=dev STACK=producer && make apply ENV=dev STACK=producer                                   # as entingestion-dev-deploy
```

## Checks (also run in CI)

```bash
# Python
cd services/producer && uv sync && uv run ruff check . && uv run mypy && uv run pytest

# Terraform (fmt, validate, terraform test, teardown script tests, tflint, checkov) - no AWS access needed
make -C terraform check
```

## Dependencies and licenses

Only open-source packages are used, and CI enforces it: `tools/check_licenses.py` checks every installed package in each service.
- **Allowed:** permissive licenses (MIT, BSD, Apache-2.0, ISC, PSF, CNRI-Python, Zlib, Unlicense, CC0, HPND), and weak copyleft libraries used unmodified (LGPL, MPL).
- **Denied:** GPL, AGPL, SSPL/BUSL and commercial licenses.
- **Multiple licenses:** a package offering several licenses passes if any one of them is allowed.
- **Unknown licenses** fail until someone reviews them.

This is why PDFs use **pdfplumber** (MIT, on pdfminer.six) and not PyMuPDF (AGPL-3.0 or commercial). The only weak-copyleft runtime dependencies are psycopg (LGPL-3.0, used unmodified) and tld (MPL-1.1, via trafilatura).

```bash
cd services/<service> && uv run python ../../tools/check_licenses.py
```

## Adding a service

1. Put the application code in `services/<name>/`.
2. Create `terraform/stacks/<name>/`, composed from `terraform/modules/*`, with `backend "s3" {}` and its own `tests/`. Add a module only when nothing existing fits, and keep it generic.
3. For each environment, add `terraform/envs/<env>/<name>.tfvars` and `<name>.s3.tfbackend` (key `<env>/<name>/terraform.tfstate`).
4. Read upstream values from SSM (`/entingestion/<env>/<service>/...`).
