# Upload UI

A hosted web page for producers who don't want to use the API, the CLI or Bruno. Someone signs in, picks a source, drags in files and uploads them as one batch (a process). Then they watch each file through registration and ingestion to `INDEXED`, down to each article's steps and errors. An Operations tab shows what's failed or stuck across the account, the queue depths, search and the table registry, and can re-run articles and redrive the DLQ (see [Operations](#operations)).

There is one shared login and no Cognito. Every API call goes through one fixed backend identity, the "static user".

## Architecture

```
browser ──▶ CloudFront ── viewer-request CloudFront Function: HTTP basic auth
               │            (sha256 of the Authorization header == KeyValueStore key "auth")
               ├─ /*      ─▶ S3 site bucket (OAC)            the built SPA (services/ui/web/dist)
               └─ /api/*  ─▶ Lambda function URL (OAC, IAM)  ui-<env>-bff, the static user
                               └─ SigV4 ─▶ producer API + control API (fixed route allow-list)
browser ──PUT──▶ S3 presigned URL (raw bucket; its CORS allows the UI origin)
```

| Part | Where | What it is |
|---|---|---|
| Web app | `services/ui/web` | React and Vite. A hash router, so it's static files only |
| BFF | `services/ui/bff`, Lambda `ui-<env>-bff` | Python 3.12 and Powertools. An allow-list of routes, signed upstream with its role |
| Infrastructure | `terraform/stacks/ui` | Site bucket, CloudFront, the basic-auth function and its KeyValueStore, the BFF and its function URL |
| Hand-off | SSM `/entingestion/<env>/…` | The ui stack reads `producer/api_url`, `producer/api_execution_arn`, `producer/raw_bucket_name`, `producer/kms_key_arn`, `ingestion/control_api_url` and `ingestion/control_api_execution_arn`. It publishes `ui/origin` |

The APIs only accept `AWS_IAM` and answer no `OPTIONS`, so a browser can't call them directly. Putting the BFF on the same origin avoids CORS on the APIs entirely. File bytes never pass through it: the browser PUTs each file straight to S3 with the presigned URL from its upload ticket, as any other client does.

### Routes

The browser calls these. Anything else gets a 404. Upstream status codes and bodies come back unchanged. A network error or timeout is a 502.

| BFF route | Upstream |
|---|---|
| GET `/api/sources` | producer GET `/v1/sources` |
| GET `/api/processes?source=&limit=&next_token=` | producer GET `/v1/processes` |
| POST `/api/processes` | producer POST `/v1/processes` |
| GET `/api/processes/{id}` | producer GET `/v1/processes/{id}` |
| POST `/api/processes/{id}/close` | producer POST `/v1/processes/{id}/close` |
| POST `/api/processes/{id}/uploads` | producer POST `/v1/processes/{id}/uploads` |
| GET `/api/processes/{id}/uploads?limit=&next_token=` | producer GET `/v1/processes/{id}/uploads` |
| GET `/api/processes/{id}/summary` | control GET `/v1/processes/{id}/summary` |
| GET `/api/processes/{id}/articles` | control GET `/v1/processes/{id}/articles` |
| GET `/api/articles/{id}/status` | control GET `/v1/articles/{id}/status` |
| GET `/api/ops/overview` | control GET `/v1/ops/overview` |
| GET `/api/ops/articles?status=&limit=&next_token=` | control GET `/v1/articles` |
| GET `/api/ops/tables?tenant_id=` | control GET `/v1/tables` (read-only) |
| POST `/api/ops/search` | control POST `/v1/search` |
| POST `/api/ops/ingest` | control POST `/v1/ingest`: at most 100 `article_ids`, no `from_stage` |
| POST `/api/ops/processes/{id}/ingest` | control POST `/v1/processes/{id}/ingest` |
| POST `/api/ops/dlq/redrive` | control POST `/v1/dlq/redrive`: `max` at most 200 |

Responses and errors are as in [API.md](API.md). On the three actions (`/api/ops/ingest`, `/api/ops/processes/{id}/ingest`, `/api/ops/dlq/redrive`) the BFF checks the body before calling upstream: it must be a JSON object (malformed JSON gets 400 `{"message": "invalid JSON"}`), and a body over the caps gets 422 in the APIs' validation shape, `{"message": "request validation failed", "errors": [{"loc": ["body", "<field>"], "msg": "…"}]}`.

## Operations

The Operations tab is for whoever runs ingestion day to day. It reads account-wide state from the control API and can take three actions. Every action runs as a dry run first and shows what it would do; nothing is queued until you press **Confirm**, which sends the same request with `dry_run: false` and the same `Idempotency-Key`.

| Page | Where | What it does |
|---|---|---|
| **Overview** | `#/ops` | A card per in-progress and failed status: the count and the age of the oldest article, which links to that article. Queue depths (visible and in flight) for ingest, ingest DLQ, backfill and backfill DLQ; a queue the control API can't read shows "unavailable". Refreshes every 15 s |
| **Failed & stuck** | `#/ops/articles?status=FAILED_PERMANENT` | A status picker and a paged table: article, error code and message, process and updated time. Tick rows, then **Re-run selected** (at most 100). The preview lists what would be queued and what's missing. **Force** skips the unchanged-content check |
| **Process → Re-run failed articles** | the process page | Shown when the summary has failures. Re-runs that process's failed articles (`only_failed: true`), with the same preview and confirm |
| **Article → Re-run this article** | the article page | Re-runs one article from the beginning, with preview and confirm |
| **DLQ** | Operations → DLQ | The DLQ depth, an optional `error_code` filter and a `max` (at most 200), then **Preview redrive** and **Confirm** |
| **Search** | `#/ops/search` | Pick a tenant (from the sources the UI may use), type a query, set `k`. Results show the title, heading path, snippet, score and the vector and keyword ranks, with a link to the article page |
| **Tables** | Operations → Tables | The live table and write tables of each tenant's registry, with row counts. Read-only |

Re-runs from the UI always start from the beginning (no `from_stage`), and each article resumes as the control API decides: unchanged content is skipped unless **Force** is ticked. Model operations (reindex, switching the live table, retiring a write table) and deletes are not in the UI; they stay with the CLI and the control API ([RUNBOOK.md](RUNBOOK.md)).

## Security model

- **Login.** Every request, to the page and to `/api/*`, passes a CloudFront Function first. The function hashes the request's `Authorization` header (`Basic base64(user:password)`) with SHA-256 and compares the hash with the value stored under `auth` in a CloudFront KeyValueStore. A missing header, a wrong one, or an empty store gets a 401 with a `WWW-Authenticate` challenge, so the browser shows its login prompt.
- **Where the password lives.** Nowhere in AWS or git: `make ui-password` stores only the hash. Terraform never sees it, and the function code holds no secret. The hash is unsalted, so choose a long password (the script requires 12 characters or more).
- **The origins accept only this distribution.** The site bucket's policy lets only this distribution read it (OAC, `AWS:SourceArn`). The BFF's function URL requires IAM auth. CloudFront signs each request with SigV4 through a Lambda OAC, which replaces the browser's basic-auth header. Only this distribution may invoke the function, through `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction`. Direct calls to either origin get a 403.
- **The static user.** The BFF calls the APIs as its role, `ui-<env>-bff`.
  - **Producer API:** the role may invoke the seven producer routes above. The producer's `source_access` then decides which sources it may read or write, so list the role as a writer of the sources the UI should offer.
  - **Control API:** the role may invoke the GET routes above (status, overview, articles by status, `GET /v1/tables`) and four POSTs: `/v1/search`, `/v1/ingest`, `/v1/processes/*/ingest` and `/v1/dlq/redrive`. No DELETE, PATCH, reindex, `tables/active` or `tables/writes` route is reachable, in the BFF's code or its IAM policy.
  - **Search** checks the tenant against the producer's `source_access`: the role may search a tenant when it reads or writes one of its sources.
- **Operations are open to everyone with the login.** Anyone with the UI password can re-run articles (at most 100 per request, `force` allowed, never `from_stage`), re-run a process's failures and redrive the DLQ (`max` at most 200). The BFF enforces the caps itself and returns 422 above them; the control API validates the rest. Model changes (reindex, switching or retiring a table) and deletes remain CLI-only.
- **CSRF guard.** Browsers resend cached basic-auth credentials on cross-site requests. So every POST must carry `X-Requested-With: entingestion-ui`, which a cross-site form can't send, and when the browser sends `Sec-Fetch-Site` it must be `same-origin`. Otherwise the BFF returns 403.
- **Path ids** must match `^[A-Za-z0-9_-]{1,128}$`, otherwise 400. The query string is forwarded only on the list routes: `/api/processes`, `/api/processes/{id}/uploads`, `/api/ops/articles` and `/api/ops/tables`.
- **Response headers.** On every response:
  - a CSP: `default-src 'self'`, with `connect-src` adding only the raw bucket's regional endpoint, `frame-ancestors 'none'` and `'wasm-unsafe-eval'` so the page can hash large files in WebAssembly;
  - HSTS for a year;
  - `nosniff`, `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`.
- **Logs.** The BFF logs the method, the route, the upstream status and the duration. Each Operations action is also logged as `ops action` with its route, path ids, `dry_run`, the number of `article_ids` and the status, so the BFF's log group records what the UI did and when. It never logs request or response bodies.

## First-time setup

The producer stack reads the UI origin for its raw bucket's CORS, and the ui stack reads both APIs' URLs, so the first apply goes around once:

```bash
aws login --profile entingestion
make -C terraform build                                     # every Lambda zip, the BFF's included
make -C terraform deploy ENV=dev STACK=producer             # 1. publishes the API URL; CORS: localhost only
make -C terraform deploy ENV=dev STACK=ingestion            # 2. publishes the control API URL
make -C terraform deploy ENV=dev STACK=ui                   # 3. CloudFront, BFF; publishes ui/origin
make -C terraform deploy ENV=dev STACK=producer             # 4. the raw bucket's CORS now allows the UI
make -C terraform ui-password ENV=dev                       # set the login
make -C terraform ui-deploy ENV=dev                         # build and upload the web app
make -C terraform ui-url ENV=dev                            # open this
```

Before the first upload, add the BFF role to the producer's `source_access` writers of each source the UI should offer. In `envs/<env>/producer.tfvars` that's `arn:aws:iam::<account>:role/ui-<env>-bff`, which the ui stack's `bff_role_arn` output prints. Then apply the producer again. Without this, `/api/sources` is empty and creating a process gets a 403.

The deploy roles may create `ui-<env>-*` roles only once the bootstrap change that allows them is applied ([terraform/bootstrap](../terraform/bootstrap/README.md)). Until then dev runs with `workload_boundary_enabled = false`, as the other stacks do.

A new CloudFront distribution takes a few minutes to deploy. Until `ui-password` has run, every request gets a 401.

## Day to day

| Command | Does |
|---|---|
| `make -C terraform ui-password ENV=<env>` | Prompts for a username and password (not echoed) and stores `sha256("Basic " + base64(user:password))` in the KeyValueStore. Use it to rotate too: it takes effect at every edge within seconds, with no apply. Browsers prompt again once their cached login fails |
| `make -C terraform ui-deploy ENV=<env>` | `npm ci` and `npm run build`, then uploads `dist/` to the site bucket and invalidates `/index.html`. Hashed files under `assets/` are cached for a year (`immutable`); `index.html` and the other files are `no-cache`, so a new build shows on the next load |
| `make -C terraform ui-url ENV=<env>` | Prints `https://<id>.cloudfront.net` |
| `make -C terraform ui-dev ENV=<env>` | Runs Vite on `http://localhost:$UI_DEV_PORT` (default 5173), proxying `/api` to that environment's CloudFront with basic auth from `UI_USER` and `UI_PASSWORD`. The dev raw bucket's CORS allows `http://localhost:5173` (`ui_extra_origins` in `envs/dev/producer.tfvars`): with another `UI_DEV_PORT`, add that origin there and apply the producer, or uploads fail CORS |

```bash
export UI_USER=ops UI_PASSWORD='…'       # the login set with ui-password
make -C terraform ui-dev ENV=dev         # UI_DEV_PORT=<port> for another port (see above)
```

A BFF code change is a normal stack deploy: run `services/ui/bff/scripts/build_lambda.sh`, then `make deploy ENV=<env> STACK=ui`. It rolls back with `make rollback ENV=<env> FN=ui-<env>-bff VERSION=<n>`.

### End-to-end check

`services/ui/bff/scripts/ui_acceptance.py` drives the deployed UI's path end to end, covering:
- **the login gate:** 401s, and 403 on the direct function URL;
- **the security headers;**
- **the CSRF guard and the route allow-list;**
- **one batch of 3 small files:** ticket, S3 CORS preflight, presigned PUT, close, and polling until COMPLETE with 3 INDEXED;
- **Operations:**
  - the overview (status and queue keys) and the FAILED_PERMANENT list;
  - search finds the samples in their source's tenant;
  - a dry-run re-run of one article counts 1, then a real `force` re-run reaches INDEXED again with a new run;
  - a dry-run "re-run failed" of the process counts 0, and a dry-run DLQ redrive answers 200;
  - the caps: 101 ids or a `from_stage` gets 422, and `/api/ops/reindex` gets 404;
- **cleanup:** it deletes the articles again, signed with your own AWS credentials.

```bash
cd services/ui/bff
UI_ENV=dev UI_USER=… UI_PASSWORD='…' uv run python scripts/ui_acceptance.py
```

Its settings come from the environment, else from the Terraform outputs of the `UI_ENV` workspace (`stacks/<stack>/.terraform/<env>`, which a `make deploy` or `make init ENV=<env> STACK=<stack>` from this checkout sets up):

| Variable | Else |
|---|---|
| `UI_USER`, `UI_PASSWORD` | required: the login set with `ui-password` |
| `UI_URL` | the ui stack's output `url` |
| `UI_BFF_FUNCTION` | the ui stack's output `bff_function_name` |
| `PRODUCER_API_URL` | the producer stack's output `api_url` |
| `UI_SOURCE` | `acceptance`: the source the batch writes; the BFF's role must be one of its writers |
| `AWS_REGION` | needed only when `PRODUCER_API_URL` carries no region (a custom domain); the region is otherwise taken from the URL |

## Limits

- **One shared login.** Everyone who knows it is the same user. There's no per-person audit: the producer records the BFF role as `created_by` of every process and upload.
- **Every source the BFF may write is open to everyone with the login,** and so are the Operations actions: re-runs and DLQ redrives, within the caps.
- **Files of at most 5 GiB:** one presigned PUT per file, with no multipart upload.
- **Basic auth:** no logout, no lockout or rate limit on wrong passwords (there's no WAF in dev), and browsers cache the credentials until they close.
- **The CloudFront domain** (`*.cloudfront.net`) with the default certificate, so the minimum TLS version can't be pinned.
- **No CloudFront access logs:** the BFF's log group records every API call, but page loads aren't logged.

## Follow-ups

- **Cognito:** a login per person, groups mapped to sources, and the user's identity passed to the producer.
- **Prod prerequisites:**
  - a WAF web ACL on the distribution, with rate limiting and the managed rule groups;
  - CloudFront access logs;
  - a custom domain with an ACM certificate (TLS 1.2 minimum).

  Prod is plan-only until these are done.
- **Multipart upload** for files over 5 GiB.
