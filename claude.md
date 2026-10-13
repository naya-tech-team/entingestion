# entingestion

Article ingestion on AWS: a producer registers uploads, an ingestion pipeline indexes them in PostgreSQL + pgvector, and an upload UI sits in front. Docs: [docs/](docs/README.md).

## Layout

- `services/producer`: producer API, registrar, janitor (Python, uv)
- `services/ingestion`: ingestion pipeline, control API, reconciler, migrations (`db/migrations`), operator scripts
- `services/ui/bff` (Python backend for the UI) and `services/ui/web` (React + Vite)
- `services/common`: shared package `entcommon` (authz, paging, DynamoDB helpers, SigV4, time), a path dependency of the three Python services
- `terraform/bootstrap` (state bucket, deploy roles; once per account), `terraform/stacks/{producer,ingestion,ui}`, `terraform/modules/*` (generic), `terraform/envs/<env>/` (`common.tfvars`, `<stack>.tfvars`, `<stack>.s3.tfbackend`)
- `bruno/`: API collection; `docs/`: architecture, workflows, runbook, diagrams; `tools/`: license gates, build and credential helpers

## Rules

- Never hard-code account ids, regions, model ids or resource names. Code reads them from env vars (each service's `config.py`), Terraform from tfvars (`envs/<env>/common.tfvars` for region and tags), scripts from Terraform outputs (`services/ingestion/scripts/_tf.py`). The account is `AWS_ACCOUNT_ID`, the state bucket's region `STATE_REGION`.
- Open-source dependencies only: no GPL, AGPL, SSPL/BUSL or commercial licenses. Gates: `tools/check_licenses.py` (Python, per service) and `tools/check_npm_licenses.mjs` (`npm run licenses` in `services/ui/web`).
- Never apply prod. Prod is plan-only.
- Migrations are expand-only. Vector tables (`chunks_<profile>`) come from `embedding_profiles` in the tfvars; never write one in SQL.

## Checks (run locally; CI is disabled: `.github/workflows/ci.yml` is commented out)

```bash
# each Python service: services/common, services/producer, services/ingestion, services/ui/bff
uv sync --all-extras && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
uv run python <path to repo>/tools/check_licenses.py
# ingestion integration tests: Docker (testcontainers), or an existing pgvector server
INGEST_TEST_PG_URL=postgresql://postgres@127.0.0.1:55432/postgres uv run pytest tests/integration
# web
cd services/ui/web && npm ci && npm run typecheck && npm test && npm run build
# Terraform (fmt, validate, tests, script tests, tflint, checkov; no AWS access)
make -C terraform check
```

The Python tests take their region from `AWS_REGION`, else `AWS_DEFAULT_REGION`, else the AWS config's region (moto works in any); they stop with a message if none is set.

Behind Zscaler: `AWS_CA_BUNDLE` (AWS CLI, boto3), `UV_SYSTEM_CERTS=1` (uv), `NODE_EXTRA_CA_CERTS` (npm). See docs/RUNBOOK.md "Behind a TLS-inspecting proxy".
