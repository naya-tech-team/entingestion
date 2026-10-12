# Ingestion pipeline

Reads the producer's `articles` stream and turns every verified file into validated, searchable chunks in Aurora PostgreSQL with pgvector. Status is tracked per article, run and step. The brief is [`/INGESTION_HANDOFF_PROMPT.md`](../../INGESTION_HANDOFF_PROMPT.md), and the infrastructure is in [`/terraform/stacks/ingestion`](../../terraform/stacks/ingestion). Operating it (alarms, replays, reindex, rollback, teardown): [`/docs/RUNBOOK.md`](../../docs/RUNBOOK.md).

```
articles stream --Pipe (filters)--> SQS ingest --> dispatcher --> Step Functions (one execution per article version per process)
  Claim -> Fetch -> Extract -> Normalize -> Chunk -> Embed -> Validate -> Index -> Finalize
  each step reads the previous step's staged output, stages its own, and updates ingestion_status
```

## Decisions that differ from the brief

| Brief | Here | Why |
|---|---|---|
| Execution name `{article_id}#{sha16}#{pipeline_version}` | `{article_id}-{sha256(content, process, source_updated_at, pipeline_version)[:32]}` | Step Functions rejects `#`. The brief's key would also drop unchanged articles joining a new process, and PATCHes, which UC3/UC11 must count. |
| Process roll-up on LATEST transitions | One membership item per (process, article) | An article can belong to several processes, and counting on LATEST transitions under- or double-counts. |
| Dispatcher concurrency sized to Bedrock | Embed's concurrency bounds Bedrock calls | The dispatcher's concurrency only limits how fast executions *start*. |
| Model tokenizer | `tiktoken cl100k_base`, bundled in the zip | Titan's tokenizer isn't published. Bedrock's own token count is validated against the 8,192 limit. |
| `docs` group includes Embed | Embed is in `core` | pyarrow is 150 MB, and `docs` with it exceeded Lambda's 250 MB limit. |
| Validate sets STAGED when it starts | VALIDATING while running, STAGED on success | The reconciler re-indexes anything STAGED for 30 minutes. |
| PDF -> PyMuPDF | PDF -> pdfplumber (MIT) | Open-source-only policy: PyMuPDF is AGPL-3.0 or commercial. |
| Aurora + RDS Proxy everywhere | `database_mode`: dev = one RDS PostgreSQL instance (no proxy), prod = Aurora + Proxy | The AWS free plan allows Aurora only as "express" clusters (outside the VPC, not in Terraform) and has no RDS Proxy. Same IAM-token auth either way. |

## Retrieval

`POST /v1/search` on the control API searches one tenant: the caller is authorized against the producer's `source_access`, the control API embeds the query, and the `query` function (in the VPC, `ingest_reader` under row-level security for that tenant) fuses vector and keyword candidates by reciprocal rank; Bedrock reranking is optional (`rerank_enabled`). Migrations 005 (tenants, RLS, filter columns) and 006 (weighted `content_tsv`, `parent_text`, citation offsets) back it. Evaluation is chunk-level with confidence intervals, and switching the live table is gated on it. Details: [ARCHITECTURE.md §8](../../docs/ARCHITECTURE.md), [API.md](../../docs/API.md).

| Module | Does |
|---|---|
| `query.py` | `search_hybrid` (tenant-scoped), and the internal `search`/`counts`/`versions` (all tenants) |
| `search.py` | The control API's search: embed, call `query`, optional rerank |
| `access.py` | The producer's source access policy (who may search a tenant, each source's PII policy) |
| `evaluation.py` | Golden-set scoring, reports, the switch gate |
| `pii.py` | Optional Comprehend PII screening in Normalize |
| `lifecycle.py` | Purging a deleted article's staged files and cache entries; retiring old versions |

## Development

```bash
uv sync --all-extras
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest            # pgvector integration tests need Docker (they run in CI), or
INGEST_TEST_PG_URL=postgresql://postgres@127.0.0.1:5432/postgres uv run pytest tests/integration
```

`tests/unit/test_pipeline.py` runs the real step handlers in state-machine order against moto, with Bedrock stubbed and pgvector replaced by an in-memory store. It asserts the brief's record counts per run (§6.3) and scenarios UC1-3, UC5, delete, DUP and CONC.

## Build and deploy (dev)

```bash
scripts/build_lambda.sh all                                   # dist/core.zip, dist/docs.zip
make -C ../../terraform plan  ENV=dev STACK=ingestion
make -C ../../terraform apply ENV=dev STACK=ingestion
make -C ../../terraform migrate ENV=dev                       # schema + DB users (ingestion-dev-migrate:live)
```

The two packages are reproducible: the same `uv.lock` and source give the same zip. The build fails if a package exceeds Lambda's 250 MB unzipped limit. `core` bundles the migrations and the RDS CA bundle (`certs/`); `docs` bundles the tokenizer encoding file.

Database access needs no stored passwords. In-VPC functions connect through RDS Proxy with IAM tokens (end-to-end IAM auth). Only `migrate` uses the master secret, through the Secrets Manager VPC endpoint. The control API's table status and switch run in `tables` as `ingest_ops` (migration 004), which can read the vector tables and replace the `chunks_active` view (owned by `ingest_views`) but can't write rows. `migrate` refuses `truncate` where `ALLOW_TRUNCATE` is false (prod).
