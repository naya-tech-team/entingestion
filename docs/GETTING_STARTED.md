# Getting started

How to go from an empty AWS account to an indexed document, step by step. Each step says how to check it worked. Allow about an hour; most of it is Terraform creating the database and VPC.

## 1. Install the tools

| Tool | Version | Used for |
|---|---|---|
| AWS CLI | v2 | Credentials, invoking functions, teardown |
| Terraform | ≥ 1.10 | Infrastructure |
| uv | recent | Python 3.12 environments and builds |
| tflint | the version in `terraform/Makefile` (`TFLINT_VERSION`) | `make check` |
| Node.js | ≥ 22.12 | The upload UI's web app (`services/ui/web`), the CloudFront Function tests, the Bruno CLI (`npx @usebruno/cli`) |
| Bruno | 4.x (optional) | API collection in `bruno/` |
| draw.io desktop | optional | Editing `docs/diagrams/entingestion.drawio` |
| Docker | optional | pgvector integration tests (testcontainers), unless you point `INGEST_TEST_PG_URL` at a server |

Check: `aws login --profile entingestion` succeeds, `aws sts get-caller-identity --profile entingestion` prints your account, and `terraform version` is 1.10 or newer.

## 2. Prepare the account

1. **Region:** the stacks deploy to `region` in `terraform/envs/<env>/common.tfvars`; change it there. The Terraform state bucket has its own region, the one you bootstrap in (step 3).
2. **Bedrock model access:** in the Bedrock console, enable the model of every `bedrock` profile in `embedding_profiles` (`terraform/envs/<env>/ingestion.tfvars`; the tracked files use Amazon Titan Text Embeddings V2) and `rerank_model_id` if you turn reranking on. Also enable a text model if you will generate the synthetic golden set (you name it with `--model` or `GOLDEN_MODEL_ID`).
3. **Account plan:**
   - On the AWS free plan, use dev's settings: `database_mode = "rds"` and `ocr_enabled = false`.
   - Prod needs a paid plan: Aurora, RDS Proxy and Textract aren't available on the free plan.
4. **Mandatory tags:** every resource carries the organization's tags: Name, Created By, Created Date, Owner, Internal Client Code, Client, Project Name and Division. Fill in `mandatory_tags` in `terraform/bootstrap/terraform.tfvars` and in each `terraform/envs/<env>/common.tfvars` (shared by every stack of the environment); a plan stops while any value is empty. `created_date` is the date of the stack's first apply in that environment (YYYY-MM-DD). `Name` comes from the resource itself (VPC, subnets, security groups, databases), or else the stack (`producer-<env>`, `ingestion-<env>`, `entingestion-bootstrap`).
5. **Lambda concurrency:** new accounts start at 10 concurrent executions. Dev works with that, but slowly. Prod needs an increase first: see [RUNBOOK.md, Prerequisites](RUNBOOK.md#prerequisites-for-a-new-environment).
6. **Credentials:** no access keys. Sign in with MFA and use `aws login`; Terraform runs as a per-environment deploy role. Set it up once with [AWS_ACCESS.md](AWS_ACCESS.md). Then, to call the APIs, attach these output policies to your principal:
   - the producer's `invoke_policy_arn`;
   - the ingestion `control_api_operator_policy_arn`, or `control_api_reader_policy_arn` for read-only access.

## 3. Create the Terraform state bucket (once per account)

```bash
aws login --profile entingestion
export AWS_ACCOUNT_ID=<12-digit account> TF_VAR_aws_account_id=<same>
export AWS_REGION=<state region> TF_VAR_region=<same>          # where the state bucket goes
export TF_VAR_deploy_principal_arns='["user/<you>"]'
cd terraform/bootstrap
AWS_PROFILE=entingestion terraform init && AWS_PROFILE=entingestion terraform apply
```

This creates the deploy roles (`entingestion-<env>-deploy`, see [AWS_ACCESS.md](AWS_ACCESS.md)), and `entingestion-tfstate-<account>-<region>`, a versioned, KMS-encrypted bucket. It holds every environment's state, at `<env>/<stack>/terraform.tfstate`. No file names the account or the region: they come from `AWS_ACCOUNT_ID` and `TF_VAR_region`. Keep these exported for every `make` target:
- `AWS_ACCOUNT_ID`: the Makefile passes it to Terraform, builds the state bucket and key names from it, and refuses credentials from any other account. The ARNs in the tfvars are derived from it (`role/<name>` → `arn:aws:iam::<account>:role/<name>`).
- `STATE_REGION` (default `AWS_REGION`): the region you just bootstrapped in, so the Makefile finds the bucket. It may differ from the stacks' region in `common.tfvars`: an account bootstrapped in `us-east-1` needs `STATE_REGION=us-east-1` even when its stacks run in `us-west-2`.

## 4. Deploy the producer

Add the `entingestion-dev` profile from [AWS_ACCESS.md](AWS_ACCESS.md) first. `make` runs Terraform as `entingestion-dev-deploy` and refuses to run as anything else.

```bash
make -C terraform build                           # every Lambda zip, no AWS access
cd services/producer
uv sync && uv run pytest                          # optional: unit tests
make -C ../../terraform plan  ENV=dev STACK=producer
make -C ../../terraform apply ENV=dev STACK=producer
ENV=dev scripts/smoke.sh                          # uploads tests/fixtures; waits for settled
```

The smoke test ends with every fixture `STAGED` and the process `settled`. The stack publishes its hand-off values to SSM, under `/entingestion/dev/producer/`.

## 5. Deploy the ingestion pipeline

```bash
cd services/ingestion
uv sync --all-extras && uv run pytest             # optional: unit tests
make -C ../../terraform plan  ENV=dev STACK=ingestion
make -C ../../terraform apply ENV=dev STACK=ingestion
make -C ../../terraform migrate ENV=dev          # invokes the output migrate_function_name's live alias
```

The plan needs `embedding_profiles`, `default_profile` and `rerank_model_id` in `envs/dev/ingestion.tfvars` (they have no defaults). The first apply takes 15–25 minutes, mostly for the database. `migrate` runs the schema migration (extension, IAM-auth database users, shared tables), generates a vector table per embedding profile, and points `chunks_active` at the default profile's table. It's idempotent: a second call prints `"applied": []` and `"profile_tables_created": []`.

Check the stack:

```bash
make -sC ../../terraform output ENV=dev STACK=ingestion     # control_api_url, function names, ...
```

## 6. Index your first document

Pick any of these.

**A. Bruno.** Open `bruno/` in Bruno, choose the `dev` environment and run the folder **04 Use cases / UC1 New article**. Or from a terminal:

```bash
cd bruno && npx @usebruno/cli@4.1.0 run "04 Use cases/UC1 New article" --env dev
```

It creates a process, uploads a generated document, waits for registration and then for indexing. It checks LATEST INDEXED, version 1, and 8 succeeded steps. On dev that takes about a minute. The environment reads every deploy-specific value from your shell: export `PRODUCER_URL` and `INGESTION_URL` (the stack outputs `api_url` and `control_api_url`), `AWS_REGION`, `AWS_PROFILE`, `BRUNO_SOURCE`, `BRUNO_TENANT`, `LIVE_TABLE` and `NEW_TABLE` first. See [bruno/README.md](../bruno/README.md).

**B. The producer CLI** with your own files:

```bash
cd services/producer
export PRODUCER_API_URL=$(make -sC ../../terraform output ENV=dev STACK=producer | awk -F'"' '/^api_url/{print $2}')
uv run producer-cli ingest-dir ./my-docs --name "First batch" --source docs --close
```

Then follow the process with the control API: `GET /v1/processes/{id}/summary` (see [API.md](API.md)).

**C. The phase gate script** with a generated corpus. In dev, keep it small:

```bash
cd services/ingestion
uv run python scripts/sample_corpus.py /tmp/corpus --count 3
uv run python scripts/gate_phase1.py --corpus /tmp/corpus --env dev --timeout-minutes 15   # GATE PASSED
```

## 7. Look around

- **Dashboard:** CloudWatch → Dashboards → `ingestion-dev`. You'll see executions, step durations, Bedrock calls and freshness lag (stream runs only).
- **One article:** `GET /v1/articles/{id}/status` returns LATEST, its runs and steps, and a link to the Step Functions execution.
- **Staged output:** `aws s3 ls s3://ingest-staging-dev-<account>/<article_id>/v1/ --recursive`.
- **Search quality** (optional): `uv run python scripts/make_golden_set.py --env dev --model <bedrock text model id>` (or `GOLDEN_MODEL_ID`), then `uv run python scripts/evaluate.py --env dev`.

## 8. Run the checks before a change

```bash
make -C terraform check                           # fmt, validate, terraform test, script tests, tflint, checkov
for s in common producer ingestion ui/bff; do
  (cd services/$s && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest) || break
done
(cd services/ui/web && npm run typecheck && npm test && npm run build)
```

CI is disabled (`.github/workflows/ci.yml` is commented out), so these run locally. For dependency changes also run the license gates, which allow only open-source dependencies: `uv run python ../../tools/check_licenses.py` in each Python service and `npm run licenses` in `services/ui/web`.

## 9. Tear it down (disposable environments)

```bash
make -C terraform destroy-all ENV=dev DRY_RUN=1    # what would be deleted
make -C terraform destroy-all ENV=dev              # ui, ingestion, then producer
```

See [RUNBOOK.md, Tear down](RUNBOOK.md#tear-down-a-disposable-environment). `make` refuses to destroy prod.

## Troubleshooting the first deploy

| Symptom | Cause | Fix |
|---|---|---|
| `RequestEntityTooLarge` creating functions | Lambda zips over 50 MB uploaded directly | Already handled: the stack uploads them to its artifacts bucket. Re-run `make plan` |
| Index fails with `E_DB_CONN` on the first runs | IAM auth on a small RDS instance is slow when connections are cold | Retried automatically; it settles within minutes |
| Control API returns 500 during a big reindex (dev) | The account's 10-concurrency limit throttles the API function | Wait, or raise the Lambda concurrency quota |
| A scanned PDF ends `SKIPPED` | OCR is off (`ocr_enabled = false`, free plan) | Turn it on in a paid account, then re-run with `force` |
| `make plan` can't read producer SSM parameters | The producer isn't deployed in this env | Deploy the producer first (step 4) |
