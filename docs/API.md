# API reference

Two REST APIs, both regional API Gateway with `AWS_IAM` auth (SigV4, service `execute-api`) and WAF. Every request below is in the [Bruno collection](../bruno/README.md).

| API | Base URL (Terraform output) | IAM policy to attach (output) |
|---|---|---|
| Producer | `api_url` of `stacks/producer`, stage `/prod` | `invoke_policy_arn` |
| Ingestion control | `control_api_url` of `stacks/ingestion`, stage `/api` | `control_api_reader_policy_arn` (GET only) or `control_api_operator_policy_arn` (all) |

```bash
make -sC terraform output ENV=dev STACK=producer  | grep api_url
make -sC terraform output ENV=dev STACK=ingestion | grep control_api_url
uvx awscurl --service execute-api --region "$AWS_REGION" "$API/v1/tables"     # the stacks' region; any SigV4 client works
```

**Errors.** Both APIs return JSON errors:
- `{"message": …}` for 404 (unknown id or route), 409 (producer: process not OPEN) and 500;
- `{"message": "request validation failed", "errors": [{"loc": [...], "msg": …}]}` for 422.

API Gateway itself returns 403 for a missing or invalid signature, and 429 when throttled.

---

## Producer API

### `GET /v1/sources`: the sources the caller may use

```json
{"items": [{"source": "bruno", "tenant_id": "internal", "access": "write"},
           {"source": "news", "tenant_id": "t-news", "access": "read"}]}
```

Returns `200` with the sources in `source_access` the caller may write (`write`) or only read (`read`), sorted by source. Admins get every source as `write`; a caller with no access gets an empty list.

### `POST /v1/processes`: create a process

| Field | Type | Rules |
|---|---|---|
| `name` | string | 1–200 characters |
| `source` | string | `^[a-z0-9][a-z0-9._-]{0,63}$`; part of the article identity |
| `expected_count` | int? | ≥ 0; can also be set at close |
| `metadata` | object? | up to 50 string pairs |

Returns `201` with a Process:

```json
{"process_id": "prc_01M4…", "name": "Batch 1", "source": "news", "status": "OPEN",
 "expected_count": null, "registered_count": 0, "rejected_count": 0, "superseded_count": 0,
 "expired_count": 0, "tickets_issued": 0, "pending_uploads": 0, "settled": false,
 "metadata": {}, "created_by": "arn:aws:iam::…", "created_at": "…", "updated_at": "…", "closed_at": null}
```

### `GET /v1/processes/{process_id}`

Returns `200` with the Process. `settled` is true when the status is CLOSED, `pending_uploads` is 0 (every upload is registered, rejected, superseded or expired), and `tickets_issued >= expected_count`. Every process is created with both counters at 0.

### `POST /v1/processes/{process_id}/close`

The body is `{"expected_count"?: int}`. It defaults to the upload tickets issued so far (`tickets_issued`). Returns `200` with the Process (CLOSED), or `409` if it's not OPEN.

### `POST /v1/processes/{process_id}/uploads`: get an upload ticket

| Field | Type | Rules |
|---|---|---|
| `filename` | string | ≤ 255 characters, no path separators or control characters |
| `content_type` | string | `text/html`, `text/plain`, `text/markdown`, `application/pdf`, or DOCX (`application/vnd.openxmlformats-officedocument.wordprocessingml.document`) |
| `size_bytes` | int | > 0, ≤ 5 GiB (the S3 single-PUT limit) |
| `sha256` | string | 64 hex characters, the SHA-256 of the exact bytes |
| `external_id` | string? | ≤ 256 characters. With `source`, it fixes the `article_id`: re-sending updates the same article |
| `title` | string? | ≤ 1000 characters |
| `metadata` | object? | string pairs |

Returns `201` with an UploadTicket:

```json
{"upload_id": "upl_01M4…", "article_id": "art_143e…", "process_id": "prc_01M4…", "method": "PUT",
 "url": "https://<raw bucket>.s3.<region>.amazonaws.com/raw/art_…/upl_…/a.pdf?X-Amz-…",
 "headers": {"Content-Type": "application/pdf", "x-amz-checksum-sha256": "<base64>", "x-amz-meta-upload-id": "upl_…"},
 "expires_at": "…"}
```

Then `PUT` the bytes to `url` with **exactly** `headers`, and no AWS auth: the URL is presigned, and it expires after 15 minutes. S3 returns `200`. A body whose SHA-256 differs is refused by S3.

### `GET /v1/uploads/{upload_id}`

Returns `200` with the Upload: `status` is PENDING, REGISTERING, REGISTERED, SUPERSEDED, or REJECTED with a `reason`.

### `GET /v1/processes/{process_id}/uploads?limit=1..500&next_token=`

Every upload ticket issued in the process, oldest first, whatever its outcome (default `limit` 100):

```json
{"items": [{"upload_id": "upl_01M4…", "article_id": "art_143e…", "filename": "a.pdf",
            "content_type": "application/pdf", "size_bytes": 1955, "status": "REJECTED",
            "reason": "sha256_mismatch", "title": null, "external_id": "doc-1",
            "created_at": "…", "finished_at": "…"}],
 "next_token": "…" | null}
```

`status` is PENDING, REGISTERING, REGISTERED, SUPERSEDED, REJECTED or EXPIRED. Upload rows expire (DynamoDB TTL) 30 days after their outcome, so older uploads drop out of the list. It's authorized like `GET /v1/processes/{process_id}`: `404` for an unknown process, or one of a source the caller can't read.

### `GET /v1/processes/{process_id}/articles?limit=1..500&next_token=`

Returns `200` with `{"items": [Article], "next_token": "…" | null}`.

### `GET /v1/articles/{article_id}`

Returns `200` with the Article: the `articles` row (`status` is always `STAGED`), `s3_uri`, `s3_version_id`, `content_sha256`, `size_bytes`, `upload_id`, `metadata` and the timestamps.

### `PATCH /v1/articles/{article_id}`

The body is `{"title"?: string, "metadata"?: object}`, with at least one of them. Returns `200` with the Article. Ingestion treats it as unchanged content: one SKIPPED_UNCHANGED run.

### `DELETE /v1/articles/{article_id}`

Returns `204`. The stream REMOVE makes ingestion delete the article's vectors.

---

## Ingestion control API

Routes split into **readers** (GET), **searchers** (`POST /v1/search`, output `control_api_search_policy_arn`) and **operators** (everything). Every POST except search accepts `"dry_run": true`, which returns `200` with what would happen. Without it, a POST that queues work returns `202`.

### `GET /v1/articles/{article_id}/status`

Returns `200`, or `404` if ingestion has no status for the article yet.

```json
{
  "latest": {"status": "INDEXED", "stage": "FINALIZE", "doc_version": 1, "chunk_count": 4,
             "run_id": "01M4…", "process_id": "prc_…", "content_sha256": "…", "pipeline_version": "v1",
             "embedding_model": "amazon.titan-embed-text-v2:0", "staging_prefix": "art_…/v1/",
             "source_updated_at": "…", "indexed_at": "…", "error_code": null},
  "runs": [{"run_id": "01M4…", "trigger": "STREAM", "claim": "NEW", "outcome": "INDEXED",
            "execution_arn": "…", "started_at": "…", "ended_at": "…"}],
  "steps": [{"step": "FETCH", "step_status": "SUCCEEDED", "attempt": 1, "duration_ms": 71,
             "metrics": {"bytes": 1955, "kind": "text/markdown"}}, …],
  "execution": "https://<region>.console.aws.amazon.com/states/home?…"
}
```

`runs` holds the last 5 runs, newest first. `steps` holds the latest run's steps; each includes `output_uri`, `output_etag`, `metrics`, and on failure `error_class`, `error_code` and `error_msg`.

### `GET /v1/processes/{process_id}/summary`

```json
{"process_id": "prc_…", "status": "COMPLETE",
 "counts": {"indexed": 3, "skipped": 0, "failed": 0},
 "producer": {"status": "CLOSED", "expected_count": 3, "registered_count": 3, "rejected_count": 0},
 "failures_by_error_code": {}}
```

`status` is IN_PROGRESS, COMPLETE or COMPLETE_WITH_ERRORS. Returns `404` if neither ingestion nor the producer knows the process.

### `GET /v1/processes/{process_id}/articles`

Each article's ingestion status, for the articles the process last sent (an article a later process re-sent is listed under that one):

```json
{"process_id": "prc_…",
 "items": [{"article_id": "art_143e…", "status": "INDEXED", "stage": "FINALIZE", "doc_version": 1,
            "chunk_count": 4, "error_code": null, "error_msg": null, "updated_at": "…",
            "indexed_at": "…", "tenant_id": "internal"}]}
```

At most 1000 items, sorted by `article_id`. `status` is an ingestion status (RUNNING, STAGED, INDEXED, SKIPPED_*, FAILED_RETRYABLE, FAILED_PERMANENT, DELETED, …). An unknown process returns `200` with no items: its articles may not have reached ingestion yet.

### `GET /v1/articles?status=<STATUS>&limit=1..200&next_token=`

One page of the articles whose latest status is `status`, oldest first (by when they reached it; default `limit` 50):

```json
{"items": [{"article_id": "art_143e…", "status": "FAILED_PERMANENT", "stage": "EMBED",
            "process_id": "prc_…", "tenant_id": "internal", "doc_version": 2,
            "error_code": "E_UNREADABLE", "error_msg": "…", "updated_at": "…"}],
 "next_token": "…" | null}
```

`status` is any ingestion status (CLAIMED … INDEXING, STAGED, INDEXED, SKIPPED, SKIPPED_UNCHANGED, FAILED_RETRYABLE, FAILED_PERMANENT, DELETED). Pass `next_token` back with the same `status` to get the next page; it's `null` on the last page, though a full page can end with a token whose next page is empty. A missing or unknown `status`, a `limit` outside 1..200, or a `next_token` that is garbled or from another status returns `422` (`{"message": "request validation failed", "errors": [{"loc": ["query", "status"], "msg": "…"}]}`).

### `GET /v1/ops/overview`

What's in flight or failed, and the queue depths:

```json
{"statuses": {"CLAIMED": {"count": 0, "oldest_at": null, "oldest_article_id": null},
              "EMBEDDING": {"count": 3, "oldest_at": "2026-10-11T03:00:00.000Z", "oldest_article_id": "art_…"},
              …,
              "FAILED_PERMANENT": {"count": 2, "oldest_at": "…", "oldest_article_id": "art_…"}},
 "queues": {"ingest": {"visible": 0, "in_flight": 1}, "ingest_dlq": {"visible": 1, "in_flight": 0},
            "backfill": {"visible": 0, "in_flight": 0}, "backfill_dlq": {"error": "unavailable"}},
 "generated_at": "2026-10-12T09:00:00.000Z"}
```

`statuses` always has CLAIMED, FETCHING, EXTRACTING, NORMALIZING, CHUNKING, EMBEDDING, VALIDATING, STAGED, INDEXING, FAILED_RETRYABLE and FAILED_PERMANENT. `oldest_at` is when the longest-waiting article reached the status; list them with `GET /v1/articles?status=`. The settled statuses (INDEXED, SKIPPED*, DELETED) are left out, since counting them means paging the whole table. `queues` gives each queue's approximate `visible` (waiting) and `in_flight` (received, not yet deleted) messages. A queue the function has no URL for is left out, and one it can't read shows `{"error": "unavailable"}` instead of failing the request.

### `GET /v1/tables?tenant_id=`

```json
{"active_table": "chunks_primary",
 "tables": {"chunks_primary": {"rows": 139, "active_rows": 139, "active_articles": 27},
            "chunks_compact":  {"rows": 130, "active_rows": 130, "active_articles": 23}},
 "tenant_id": null,
 "registry": {"live": "chunks_primary", "writes": ["chunks_primary"], "epoch": 3},
 "consistent": true,
 "tenants": {"t-small": {"live": "chunks_compact", "writes": ["chunks_compact"], "epoch": 0,
                         "tenant_id": "t-small"}},
 "profiles": {"primary": {"table": "chunks_primary", "provider": "bedrock",
                              "model": "amazon.titan-embed-text-v2:0", "dimension": 1024}, …}}
```

`profiles` lists the configured embedding profiles (`embedding_profiles` in the tfvars; the models above are dev's).

`registry` is the default registry, or with `?tenant_id=` the one that governs that tenant (its own, else the default). `consistent`: for the default registry, `chunks_active` serves its live table; for a tenant's, its live table exists. `tenants` lists every tenant with its own registry (stored, or from `tenant_profiles`). A table whose profile's table doesn't exist yet shows `null` under `tables`.

### `POST /v1/ingest`: re-run articles

| Field | Type | Meaning |
|---|---|---|
| `article_ids` | string[] | 1–500 articles |
| `force` | bool | Re-run even if the content is unchanged |
| `from_stage` | string? | Start at this step (`FETCH` … `FINALIZE`) |
| `dry_run` | bool | List the messages; queue nothing |

Without `force` or `from_stage`, each article resumes after its last intact step. The response is `{"dry_run", "count", "queued" | "messages", "not_found": [...]}`.

### `POST /v1/processes/{process_id}/ingest`: re-run a process

The body is `{"only_failed"?: bool, "dry_run"?: bool}`. The response is like `/v1/ingest`, plus `process_id`.

### `POST /v1/dlq/redrive`: re-run failed articles

The body is `{"error_code"?: string, "max"?: 1..1000 (default 100), "dry_run"?: bool}`.
- It re-queues FAILED_RETRYABLE and FAILED_PERMANENT articles (optionally only one `error_code`), resuming where their staged output is intact.
- **Without `error_code`** it also moves the ingest DLQ back to the queue (`"dlq": {"messages", "moved", "task_handle"}`).

### `POST /v1/reindex`: reindex or rebuild into a vector table

| Field | Type | Meaning |
|---|---|---|
| `tenant_id` | string? | Only this tenant's articles; the table is added to this tenant's registry (giving it its own registry on its first change). Without it: the default registry's articles and registry |
| `target_table` | string? | A profile's table from `GET /v1/tables` (default: the tenant's, or the default registry's, live table) |
| `from_stage` | string | `EMBED` (re-embed: model change) or `INDEX` (reload staged vectors: rebuild, no Bedrock) |
| `filter` | object? | `{"since"?: ISO-8601, "process_id"?: string, "pipeline_version"?: string}` |
| `dry_run` | bool | Return the article count only |

Returns `202` with `{"count", "tenant_id", "target_table", "from_stage", "tables", "execution", "execution_arn"}` (`tables`: the registry after adding the table), or `422` for an unknown table or a stage other than EMBED or INDEX.

### `POST /v1/tables/active`: switch the live table

The body is `{"table": "chunks_compact", "tenant_id"?: "t-news", "force"?: false}`. Returns `200` with the new state (`active_table`, `previous`, `tenant_id`, `tables`). Switching back to the previous table is the rollback. Returns `422` for an unknown table.

Without `tenant_id` the switch is the default registry's and also points `chunks_active` at the table (the tables function). With `tenant_id` it changes only that tenant's registry (a tenant without one gets its own, copied from the default registry), and the tenant's searches read the table directly.

Returns `409` with the reason when:
- live runs (of the tenant) don't write the table yet, or some indexed article's (of the tenant; without `tenant_id`, of the default registry) current version isn't in it (`missing`, `sample`);
- the evaluation gate fails (`"gate": "evaluation"`): the table's newest report (`_eval/<table>/`) is missing or older than `EVAL_MAX_AGE_DAYS` (7), the live table has no report on the same golden set, or the table's recall@10 is more than `EVAL_TOLERANCE` (0.02) below the live table's. Run `scripts/evaluate.py --table <t> --baseline` first.

`"force": true` skips both checks (a rollback to an old table usually needs it). Evaluation reports are per table, not per tenant.

### `DELETE /v1/tables/writes/{table}?tenant_id=`: stop writing a table

Retires `table` from the (tenant's) registry: live runs stop writing it; its rows stay. Returns `200` with the registry, or `409` for the live table.

### `POST /v1/search`: search one tenant

| Field | Type | Meaning |
|---|---|---|
| `tenant_id` | string (1-128) | The tenant to search. The caller must read or write one of its sources (or be an admin) in the producer's `source_access` |
| `query` | string (1-2000) | The question or keywords |
| `k` | int 1-50 | Results (default 10) |
| `filters` | object? | `{"source"?: string, "published_after"?: "YYYY-MM-DD", "process_id"?: string}` |
| `rerank` | bool | Rerank with the Bedrock model `rerank_model_id` (tfvars) when the stack has `rerank_enabled`; ignored otherwise |

The query is embedded with the tenant's live table's embedding profile (the tenant's own registry, else the default one; `table` in the response) and searched in that table; vector and keyword candidates (headings weigh more than text) are fused by reciprocal rank. Returns `200`:

```json
{"tenant_id": "t-news", "query": "...", "k": 10, "table": "chunks_primary", "reranked": false,
 "results": [{"chunk_id": "...", "article_id": "art_...", "doc_version": 2, "title": "...",
              "text": "...", "context": "...", "heading_path": ["..."], "page_start": 3, "page_end": 3,
              "char_start": 1200, "char_end": 2310, "source": "news", "published_at": "2026-09-30",
              "score": 0.0325, "vector_rank": 1, "keyword_rank": 2}]}
```

`context` is the chunk's section-level parent when parent-child chunking is on, else the chunk's text. `char_start`/`char_end` cite the span of the article's normalized text. A caller without access to the tenant gets `403`; a bad body `422`.

---

## Events

Ingestion publishes these events (`ingest/events.py`) to the default EventBridge bus, with source `entingestion.ingestion`:

| detail-type | When | Detail |
|---|---|---|
| `article.indexed` | Finalize | `article_id`, `doc_version`, `process_id`, `chunk_count`, `pipeline_version`, `source_updated_at`, `indexed_at` |
| `process.ingested` | The process summary turns COMPLETE / COMPLETE_WITH_ERRORS | `process_id`, `status`, `indexed`, `skipped`, `failed` |

Subscribe with an EventBridge rule on `{"source": ["entingestion.ingestion"], "detail-type": ["process.ingested"]}`.
