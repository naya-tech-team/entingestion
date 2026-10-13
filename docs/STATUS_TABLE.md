# `ingestion_status` table schema

The ingestion pipeline's single DynamoDB table: one LATEST item per article, its runs and steps, process roll-ups, admission slots and configuration. This page lists every item type and attribute the code writes. The summary is in [ARCHITECTURE.md](ARCHITECTURE.md) "5. Data model".

Sources: `terraform/stacks/ingestion/main.tf` (`module "status_table"`), `services/ingestion/src/ingest/status/` (`keys.py` has the key layout), `vectorstore/registry.py`, `reconciler/app.py`.

## Table

| Setting | Value |
|---|---|
| Name | `${table_prefix}ingestion_status` |
| Keys | `pk` (S, partition), `sk` (S, sort) |
| Billing | on demand (`PAY_PER_REQUEST`) |
| TTL | `expires_at_epoch` (RUN# and step items only) |
| Encryption | the stack's customer-managed KMS key |
| Point-in-time recovery | on |
| Deletion protection | prod only |
| Stream | none |

### Global secondary indexes

All four are sparse (only items that carry the keys appear) and project `ALL`.

| Index | Partition key | Sort key | Items in it | Read by |
|---|---|---|---|---|
| `gsi1-status` | `gsi1pk` (S): the article status, or `SUMMARY#OPEN` | `gsi1sk` (S): `updated_at` (ISO 8601) | LATEST items; SUMMARY items whose process may still need completing | listing and counting articles by status (control API, UI, redrive); reconciler; open summaries |
| `gsi2-process` | `gsi2pk` (S): `process_id` | `gsi2sk` (S): `<status>#<article_id>` | LATEST items of articles last touched by a process | process re-run and summary |
| `gsi3-lease` | `gsi3pk` (S): always `LEASE` | `gsi3sk` (N): lease expiry, epoch seconds | LATEST items held by a run | reconciler: expired leases (`gsi3sk <= now`) |
| `gsi4-stepfail` | `gsi4pk` (S): `STEPFAIL#<STEP>`, or `DELETE#PENDING` | `gsi4sk` (S): when it failed / was requested (ISO 8601) | FAILED step items; LATEST items with a delete waiting for the running run | failures by step; reconciler: pending deletes |

## Item types

| Item | `pk` | `sk` | Written by | Retention |
|---|---|---|---|---|
| [LATEST](#latest) | `<article_id>` | `LATEST` | Claim, every step, finish, retry | while the article exists |
| [RUN](#run) | `<article_id>` | `RUN#<run_id>` | Claim; ended by finish / retry / handover | `RUN_TTL_DAYS` (365) |
| [Step](#step) | `<article_id>` | `RUN#<run_id>#STEP#<nn>_<STEP>` | each step's start, success, failure | `STEP_TTL_DAYS` (90) |
| [SHADOW](#shadow) | `<article_id>` | `SHADOW#<table>` | shadow runs (reindex, rebuild, drift repair) | kept; overwritten by the next shadow run |
| [SUMMARY](#summary) | `PROCESS#<process_id>` | `SUMMARY` | process roll-up | kept |
| [Membership](#membership) | `PROCESS#<process_id>` | `ARTICLE#<article_id>` | process roll-up | kept |
| [Slot](#slot) | `#SLOTS` | `<lane>#<i>` (`live#0`, `backfill#1`, ...) | dispatcher, every step, run end | reused; a missing item is a free slot |
| [Config](#config) | `#CONFIG` | `VECTOR_TABLES`, `VECTOR_TABLES#<tenant>`, `SWEEP#...`, `ALERT#FAILED_DIGEST` | registry changes, reconciler | kept |

Step sort keys, in order: `STEP#01_CLAIM`, `02_FETCH`, `03_EXTRACT`, `04_NORMALIZE`, `05_CHUNK`, `06_EMBED`, `07_VALIDATE`, `08_INDEX`, `09_FINALIZE`. A query on `pk = <article_id>, begins_with(sk, "RUN#<run_id>")` returns a run and its steps together.

### LATEST

The article's current state. One per article.

| Attribute | Type | Meaning |
|---|---|---|
| `pk`, `sk` | S | `<article_id>`, `LATEST` |
| `article_id` | S | |
| `status` | S | [Article status](#article-status) |
| `stage` | S | the step the run is in or last finished ([Steps](#steps)) |
| `run_id` | S | the run that owns the article, or last did |
| `doc_version` | N | version number, +1 per content change (0 for an orphan-delete placeholder) |
| `content_sha256` | S | checksum of the producer's content |
| `meta_sha256`, `title_sha256` | S | fingerprints of title + metadata + tenant, and of the title (decide METADATA vs CHANGED) |
| `pipeline_version` | S | pipeline version that produced the current version |
| `tenant_id` | S | whose vector-table registry applies (absent: the default) |
| `process_id` | S | producer process that last touched it |
| `source_updated_at` | S | producer's update time; an older update can't replace a newer one |
| `staging_prefix` | S | S3 prefix of this version's staged files |
| `created_at`, `updated_at` | S | ISO 8601 |
| `lease_owner` | S | run holding the lease (absent: free) |
| `lease_expires_at` | N | epoch seconds; `LEASE_SECONDS` (2100) from the last step start or heartbeat |
| `chunk_count` | N | chunks indexed (0 after a delete) |
| `indexed_at` | S | when Finalize indexed it |
| `embedding_model` | S | model of the indexed vectors |
| `metadata_updated_at` | S | last METADATA run |
| `tv_<table>` | N | `doc_version` last written to that vector table (one per table) |
| `pii_count`, `pii_types`, `pii_action` | N, L, S | Normalize's PII finding (when screening is on) |
| `error_class` | S | `transient` or `permanent` |
| `error_code`, `error_msg` | S | last failure (`error_msg` at most 1024 chars) |
| `skip_reason` | S | error code that made it SKIPPED |
| `next_retry_at` | N | epoch seconds; when the reconciler re-queues a FAILED_RETRYABLE article |
| `retry_count`, `throttle_count` | N | separate retry budgets for failures and throttling |
| `delete_requested_at`, `delete_run_id` | S | a delete waiting for the running run to hand over |
| `gsi1pk`, `gsi1sk` | S | `status`, `updated_at` |
| `gsi2pk`, `gsi2sk` | S | `process_id`, `<status>#<article_id>` |
| `gsi3pk`, `gsi3sk` | S, N | `LEASE`, `lease_expires_at`; removed when the lease is released |
| `gsi4pk`, `gsi4sk` | S | `DELETE#PENDING`, `delete_requested_at`; only while a delete waits |

Ending a run removes the lease attributes. Any outcome but FAILED_PERMANENT also clears the error and retry attributes, and DELETED clears the pending-delete ones.

### RUN

One per run (one Step Functions execution).

| Attribute | Type | Meaning |
|---|---|---|
| `pk`, `sk` | S | `<article_id>`, `RUN#<run_id>` |
| `run_id` | S | |
| `event` | S | `UPSERT` or `REMOVE` |
| `trigger` | S | `STREAM`, `RECONCILER`, `MANUAL` or `BACKFILL` |
| `requested_by` | S | caller of a replay |
| `from_stage` | S | step a replay started from |
| `force` | BOOL | forced run (e.g. orphan delete) |
| `process_id`, `tenant_id`, `content_sha256`, `pipeline_version` | S | from the message |
| `embedding_model` | S | model of the table that decides the run |
| `execution_arn` | S | Step Functions execution |
| `slot` | S | admission slot it holds (`<lane>#<i>`) |
| `started_at`, `ended_at` | S | ISO 8601 |
| `claim` | S | [Claim kind](#claim-kind) |
| `action` | S | Claim's decision: `RUN`, `SKIP`, `STALE`, `DELETE`, `DONE`, `METADATA` (a retried Claim repeats it) |
| `doc_version`, `start_at`, `staging_prefix`, `lane`, `targets` | N, S, S, S, L | the rest of Claim's decision: version, first step, staging prefix, shadow table, tables to write |
| `outcome` | S | [Run outcome](#run-outcome) |
| `expires_at_epoch` | N | TTL |

### Step

One per step of a run.

| Attribute | Type | Meaning |
|---|---|---|
| `pk`, `sk` | S | `<article_id>`, `RUN#<run_id>#STEP#<nn>_<STEP>` |
| `article_id`, `run_id` | S | |
| `step` | S | step name |
| `step_status` | S | `STARTED`, `SUCCEEDED`, `FAILED` |
| `attempt` | N | attempts so far |
| `attempts` | L | the last 5 failures: `{attempt, error_code, at}` |
| `started_at`, `ended_at` | S | ISO 8601 |
| `duration_ms` | N | |
| `input_uri`, `output_uri` | S | S3 objects read and written |
| `output_etag` | S | ETag of the output |
| `metrics` | M | step-specific counts |
| `error_class`, `error_code`, `error_msg` | S | failure (removed when the step starts again or succeeds) |
| `gsi4pk`, `gsi4sk` | S | `STEPFAIL#<STEP>`, failure time; only while FAILED |
| `expires_at_epoch` | N | TTL |

### SHADOW

A run that writes one vector table without being the article's live run. It has no GSI keys and no process roll-up.

| Attribute | Type | Meaning |
|---|---|---|
| `pk`, `sk` | S | `<article_id>`, `SHADOW#<table>` |
| `article_id`, `table_name` | S | |
| `status`, `stage`, `run_id` | S | as on LATEST |
| `doc_version` | N | the LATEST version it works on |
| `staging_prefix` | S | |
| `updated_at` | S | |
| `lease_owner`, `lease_expires_at` | S, N | as on LATEST |

### SUMMARY

Per producer process: counts of outcomes.

| Attribute | Type | Meaning |
|---|---|---|
| `pk`, `sk` | S | `PROCESS#<process_id>`, `SUMMARY` |
| `indexed`, `skipped`, `failed` | N | articles per outcome (atomic ADD) |
| `status` | S | absent while open; `COMPLETE` or `COMPLETE_WITH_ERRORS` |
| `registered_count` | N | the producer's count when it completed |
| `completed_at`, `updated_at` | S | ISO 8601 |
| `gsi1pk`, `gsi1sk` | S | `SUMMARY#OPEN`, `updated_at`; removed on completion |

### Membership

The outcome counted for one article in one process, so a re-run moves the count instead of adding to it.

| Attribute | Type | Meaning |
|---|---|---|
| `pk`, `sk` | S | `PROCESS#<process_id>`, `ARTICLE#<article_id>` |
| `kind` | S | `membership` |
| `status` | S | counter it is in: `indexed`, `skipped`, `failed` |
| `run_id`, `updated_at` | S | |

### Slot

Dispatcher backpressure. A missing item, or one without `run_id`, is free.

| Attribute | Type | Meaning |
|---|---|---|
| `pk`, `sk` | S | `#SLOTS`, `<lane>#<i>`; lanes `live` and `backfill` |
| `lane` | S | |
| `run_id` | S | run holding it |
| `expires_at` | N | epoch seconds; extended at every step |
| `acquired_at`, `released_at` | S | ISO 8601 |

### Config

| `sk` | Attributes | Written by |
|---|---|---|
| `VECTOR_TABLES` | `live` (S), `writes` (L), `epoch` (N), `updated_at`, `updated_by` | vector-table registry, default |
| `VECTOR_TABLES#<tenant>` | same, plus `tenant_id` | a tenant's own registry |
| `SWEEP#GONE#<status>` | `cursor` (S, JSON key), `updated_at` | reconciler |
| `SWEEP#DRIFT#<table>` | `after` (S), `updated_at` | reconciler |
| `SWEEP#COVER#<table>` | `cursor` (S, JSON key), `updated_at` | reconciler |
| `ALERT#FAILED_DIGEST` | `since` (S), `updated_at` | reconciler's failure digest |

The registry's `epoch` makes changes conditional (`epoch = :seen`). `migrate` reads `VECTOR_TABLES` with `GetItem`, limited to `pk = #CONFIG`.

## Values

### Article status

`CLAIMED`, `FETCHING`, `EXTRACTING`, `NORMALIZING`, `CHUNKING`, `EMBEDDING`, `VALIDATING`, `STAGED`, `INDEXING`, `INDEXED`, `SKIPPED_UNCHANGED`, `SKIPPED`, `FAILED_RETRYABLE`, `FAILED_PERMANENT`, `DELETED`.

Terminal: `INDEXED`, `SKIPPED_UNCHANGED`, `SKIPPED`, `FAILED_PERMANENT`, `DELETED`. Roll-up counters: INDEXED → `indexed`; SKIPPED and SKIPPED_UNCHANGED → `skipped`; FAILED_PERMANENT → `failed`.

### Steps

| Step | Status while running |
|---|---|
| CLAIM | CLAIMED |
| FETCH | FETCHING |
| EXTRACT | EXTRACTING |
| NORMALIZE | NORMALIZING |
| CHUNK | CHUNKING |
| EMBED | EMBEDDING |
| VALIDATE | VALIDATING, then STAGED on success |
| INDEX | INDEXING |
| FINALIZE | INDEXING, then INDEXED |

### Claim kind

`NEW`, `CHANGED`, `UNCHANGED`, `METADATA`, `DELETE`.

### Run outcome

`INDEXED`, `SKIPPED_UNCHANGED`, `METADATA_UPDATED`, `SKIPPED`, `DELETED`, `FAILED_RETRYABLE`, `FAILED_PERMANENT`, `STALE` (lost the lease), `SUPERSEDED` (older than the article), `DELETE_DEFERRED` (handed to the running run), `PREEMPTED` (handed the article to a pending delete).

## Access patterns

| Need | Operation |
|---|---|
| An article's state | `GetItem pk=<article_id>, sk=LATEST` |
| A run and its steps | `Query pk=<article_id>, begins_with(sk, "RUN#<run_id>")` |
| An article's runs, newest first | `Query pk=<article_id>, begins_with(sk, "RUN#")`, descending; drop `#STEP#` items (run ids are ULIDs) |
| Articles by status, newest or in a time window | `Query gsi1-status: gsi1pk=<status>`, range on `gsi1sk` |
| Open process summaries | `Query gsi1-status: gsi1pk=SUMMARY#OPEN` |
| A process's articles | `Query gsi2-process: gsi2pk=<process_id>` |
| Expired leases | `Query gsi3-lease: gsi3pk=LEASE, gsi3sk <= now` |
| Failures of a step | `Query gsi4-stepfail: gsi4pk=STEPFAIL#<STEP>` |
| Pending deletes | `Query gsi4-stepfail: gsi4pk=DELETE#PENDING` |
| A process summary | `GetItem pk=PROCESS#<pid>, sk=SUMMARY` |
| A lane's slots | `Query pk=#SLOTS, begins_with(sk, "<lane>#")` (consistent) |
| Registries | `Query pk=#CONFIG, begins_with(sk, "VECTOR_TABLES")` (consistent) |
