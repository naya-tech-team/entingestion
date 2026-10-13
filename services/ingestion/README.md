# Ingestion pipeline

Reads the producer's `articles` stream and turns every verified file into validated, searchable chunks in Aurora PostgreSQL with pgvector. Status is tracked per article, run and step. The infrastructure is in [`/terraform/stacks/ingestion`](../../terraform/stacks/ingestion). Operating it (alarms, replays, reindex, rollback, teardown): [`/docs/RUNBOOK.md`](../../docs/RUNBOOK.md).

```
articles stream --Pipe (filters)--> SQS ingest --> dispatcher --> Step Functions (one execution per article version per process)
  Claim -> Fetch -> Extract -> Normalize -> Chunk -> Embed -> Validate -> Index -> Finalize
  each step reads the previous step's staged output, stages its own, and updates ingestion_status
```

## Design decisions

| Choice | Why |
|---|---|
| Execution name `{article_id}-{sha256(content, process, source_updated_at, pipeline_version)[:32]}` | Step Functions rejects `#`, and an unchanged article joining a new process, or a PATCH, must still get its own run (UC3/UC11). |
| One membership item per (process, article) for the process roll-up | An article can belong to several processes; counting on LATEST transitions under- or double-counts. |
| Embed's concurrency bounds the model calls | The dispatcher's concurrency only limits how fast executions *start*. |
| `tiktoken cl100k_base`, bundled in the zip | Titan's tokenizer isn't published. Bedrock's own token count is validated against the model's limit. |
| Embed is in the `core` package | pyarrow is 150 MB, and `docs` with it exceeded Lambda's 250 MB limit. |
| VALIDATING while Validate runs, STAGED on success | The reconciler re-indexes anything STAGED for `RECONCILE_STAGED_GRACE_SECONDS`. |
| PDF -> pdfplumber (MIT) | Open-source-only policy: PyMuPDF is AGPL-3.0 or commercial. |
| `database_mode`: dev = one RDS PostgreSQL instance (no proxy), prod = Aurora + Proxy | The AWS free plan allows Aurora only as "express" clusters (outside the VPC, not in Terraform) and has no RDS Proxy. Same IAM-token auth either way. |

## Database schema

One migration, [`db/migrations/001_schema.sql`](db/migrations/001_schema.sql), holds everything the vector tables share: the `vector` extension, the roles (`ingest_writer`, `ingest_reader`, `ingest_ops`, `ingest_views`), `article_tombstones`, `profile_tables` and the schema grants. **No vector table is written in SQL.** The migrate function then:

1. creates `chunks_<profile>` for every configured embedding profile that has no table yet, the default profile's included, from one DDL generator ([`vectorstore/profile_tables.py`](src/ingest/vectorstore/profile_tables.py)) with the profile's dimension: `halfvec(<dimension>)`, the weighted `content_tsv`, the partial HNSW index, row-level security and grants. Each table is recorded in `profile_tables`. A profile's dimension can't change: a new dimension is a new profile, so a new table;
2. recreates `chunks_active` (`security_invoker`, owned by `ingest_views`) over the default registry's live table, read from DynamoDB (`#CONFIG` only; a failed read stops migrate). Before any switch has recorded one: the table it already serves, or on a new database the default profile's table.

A schema change is a new numbered migration; a new model is a new profile in `embedding_profiles` followed by `make migrate`.

## Staged files

Per article version, under `<article_id>/v<doc_version>/` in the staging bucket:

| Step | File |
|---|---|
| Extract (or OCR) | `extracted/content.json` |
| Normalize | `normalized/document.json` |
| Chunk | `chunks/chunks.jsonl` |
| Embed | `embeddings/<table>.parquet`, one per vector table the run writes |
| Validate | `_manifest.<table>.json`, one per vector table; Index loads only a passing one |

No table name is special: re-embedding into a new table never overwrites what the current table is rebuilt from. Embedding-cache keys are `sha256(embed_text)#<model>@<dimension>@<norm|raw>#<profile fingerprint>`.

## Retrieval

`POST /v1/search` on the control API searches one tenant: the caller is authorized against the producer's `source_access`, the control API embeds the query with the tenant's live table's profile, and the `query` function (in the VPC, `ingest_reader` under row-level security for that tenant) fuses vector and keyword candidates by reciprocal rank. Bedrock reranking is optional (`RERANK_ENABLED`). Evaluation is chunk-level with confidence intervals, and switching the live table is gated on it. Details: [ARCHITECTURE.md, "Retrieval, embedding models, reindex and evaluation"](../../docs/ARCHITECTURE.md), [API.md](../../docs/API.md).

| Module | Does |
|---|---|
| `query.py` | `search_hybrid` (tenant-scoped), and the internal `search`/`counts`/`versions` (all tenants) |
| `search.py` | The control API's search: embed, call `query`, optional rerank |
| `access.py` | The producer's source access policy (`entcommon.authz`), cached per warm Lambda |
| `evaluation.py` | Golden-set scoring (chunk-labelled sets only), reports, the switch gate |
| `pii.py` | Optional Comprehend PII screening in Normalize |
| `lifecycle.py` | Purging a deleted article's staged files and cache entries; retiring old versions |

## Code layout

| Package | Holds |
|---|---|
| `config.py` | `Settings`, grouped into sections (`db`, `queues`, `chunking`, `ocr`, `search`, `access`, `pii`, `evaluation`, `admission`, `retry`, `reconcile`, `retention`, `embedding`); every environment read goes through it |
| `status/` | `store.py`, the `StatusStore` facade, built from `base`, `reads`, `slots`, `rollup`, `claim` and `steps`; pure rules in `claim_rules.py` |
| `control_api/` | `app.py` (resolver, error handlers, `handler`), `http.py` (responses, bodies, parameters, page tokens) and one Powertools Router per area in `routes/` (`runs`, `tables`, `reads`, `ops`) |
| `steps/` | One handler per step, all on the write protocol in `steps/protocol.py` |

Shared code (ARN matching, page tokens, DynamoDB helpers, timestamps) comes from [`services/common`](../common) (`entcommon`).

## Configuration

Terraform sets the deployment's values; tunables have code defaults. There are **no built-in embedding profiles**: a function without `EMBEDDING_PROFILES` and `DEFAULT_PROFILE` fails at its first invocation with a configuration error. Each handler names the settings it can't run without (`REQUIRED`) and fails on its first invocation when one is missing. Switches read `true`/`false` (also `1`/`0`, `yes`/`no`, `on`/`off`); anything else is an error.

| Variable | Default | Does |
|---|---|---|
| `EMBEDDING_PROFILES` | required | JSON: profile name -> provider, model, dimension, ... (`embedding/profiles.py`) |
| `DEFAULT_PROFILE` | required | The default registry's profile when it has no item |
| `TENANT_PROFILES` | `{}` | JSON: tenant -> its first profile |
| `ALLOW_TRUNCATE` | `false` | The migrate function's `truncate` action (rebuild test) |
| `OCR_ENABLED` | `false` | Textract OCR for scanned and large PDFs |
| `ACCESS_CACHE_SECONDS` | `300` | How long a warm Lambda keeps the source access policy |
| `DB_STATEMENT_TIMEOUT_MS` | `30000` | PostgreSQL `statement_timeout` of the pipeline's connections |
| `DB_CONNECT_TIMEOUT_SECONDS` | `30` | Connect timeout (IAM token checks can be slow on a cold instance) |
| `MIGRATIONS_DIR` | bundled | The migrations the migrate function applies |
| `DB_CA_BUNDLE` | bundled | The RDS CA bundle the migrate function verifies the writer with |
| `SEARCH_RRF_K` | `60` | Reciprocal rank fusion constant |
| `SEARCH_MIN_POOL` | `50` | Candidates per list (vector, keyword) before fusion, at least |
| `SEARCH_MIN_EF_SEARCH` / `SEARCH_MAX_EF_SEARCH` | `100` / `1000` | Bounds of HNSW `ef_search` (2 x candidates) |
| `SEARCH_RERANK_POOL` | `2` | Candidates per wanted result when reranking |
| `SEARCH_MAX_CANDIDATES` | `100` | Most candidates asked of the query function when reranking |
| `SEARCH_RERANK_TEXT_CHARS` | `4000` | Characters of each candidate sent to the reranker |
| `DISPATCH_SLOT_POLL_SECONDS` | `2.0` | How often a dispatcher checks for a free admission slot |
| `DISPATCH_BUSY_VISIBILITY_SECONDS` | `30` | When a record that found no slot is visible on its queue again |
| `RETRY_MAX_RETRYABLE` | `5` | Transient failures (after the state machine's retries) before FAILED_PERMANENT |
| `RETRY_MAX_THROTTLED` | `20` | Throttled runs before FAILED_PERMANENT (its own budget) |
| `RETRY_BASE_SECONDS` | `900` | Backoff of a transient failure: base x 2^retries |
| `RETRY_THROTTLE_SECONDS` | `300` | Backoff of a throttled run: base x 2^min(n, 3) |
| `RECONCILE_MAX_RETRIES` | `5` | Retries before the reconciler escalates a FAILED_RETRYABLE article |
| `RECONCILE_STAGED_GRACE_SECONDS` | `1800` | STAGED this long is re-indexed |
| `RECONCILE_STREAM_GRACE_SECONDS` | `300` | A producer update younger than this may still be in the Pipe |

The other variables (`LIVE_SLOTS`, `SLOT_WAIT_SECONDS`, `CHUNK_*`, `EVAL_*`, `PII_*`, `RERANK_*`, the `*_TTL_DAYS`, names and URLs) are listed in [`config.py`](src/ingest/config.py) with their defaults.

## Development

The checks run locally (CI is disabled):

```bash
UV_SYSTEM_CERTS=1 uv sync --all-extras
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest            # pgvector integration tests need Docker (testcontainers), or
INGEST_TEST_PG_URL=postgresql://postgres@127.0.0.1:55432/postgres uv run pytest tests/integration
uv run python ../../tools/check_licenses.py
```

The tests run with two Titan v2 profiles, `primary` (1024) and `compact` (512), set by `tests/conftest.py`. The integration tests prepare each database as the migrate function does (migrations, profile tables, `chunks_active`).

`tests/unit/test_pipeline.py` runs the real step handlers in state-machine order against moto, with Bedrock stubbed and pgvector replaced by an in-memory store. It asserts the status records each run writes and scenarios UC1-3, UC5, delete, DUP and CONC ([USE_CASES.md](../../docs/USE_CASES.md)).

## Build and deploy (dev)

```bash
scripts/build_lambda.sh all                                   # dist/core.zip, dist/docs.zip
make -C ../../terraform plan  ENV=dev STACK=ingestion
make -C ../../terraform apply ENV=dev STACK=ingestion
make -C ../../terraform migrate ENV=dev                       # schema, profile tables, chunks_active
```

The two packages are reproducible: the same `uv.lock` and source give the same zip. The build fails if a package exceeds Lambda's 250 MB unzipped limit. `core` bundles the migrations and the RDS CA bundle (`certs/`); `docs` bundles the tokenizer encoding file.

Database access needs no stored passwords. In-VPC functions connect through RDS Proxy with IAM tokens (end-to-end IAM auth). Only `migrate` uses the master secret, through the Secrets Manager VPC endpoint. The control API's table status and switch run in `tables` as `ingest_ops`, which can read the vector tables and replace the `chunks_active` view (owned by `ingest_views`) but can't write rows. `migrate` refuses `truncate` unless `ALLOW_TRUNCATE` is true.

## Operator scripts

The scripts read every deployed name from Terraform outputs (`scripts/_tf.py`: `tf_output`, `tf_json`, `function_name` from the `function_names` map), never from a naming convention. `--env` defaults to `$ENV`, else `dev`.

| Script | Does |
|---|---|
| `gate_phase1.py --corpus <dir>` | Ingests a corpus through the producer and checks the counts (`--source`, or `$GATE_SOURCE`) |
| `acceptance.py`, `acceptance_phase2.py`, `acceptance_phase3.py`, `load_test.py` | Scenario checks against a deployed environment (source `$ACCEPTANCE_SOURCE`, default `acceptance`). The API region comes from its URL, else `AWS_REGION` / `AWS_DEFAULT_REGION` |
| `make_golden_set.py --model <id>` | A new synthetic golden set; the question-writing model is required (`--model` or `$GOLDEN_MODEL_ID`) |
| `evaluate.py` | Runs the evaluation function (`--table`, `--baseline`) |
| `sample_corpus.py` | Generates a synthetic corpus |
