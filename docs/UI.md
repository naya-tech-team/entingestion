# Upload UI

A hosted web page for producers who don't want to use the API, the CLI or Bruno. Someone signs in, picks a source, drags in files and uploads them as one batch (a process). Then they watch each file through registration and ingestion to `INDEXED`, down to each article's steps and errors.

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

Responses and errors are as in [API.md](API.md).

## Security model

- **Login.** Every request, to the page and to `/api/*`, passes a CloudFront Function first. The function hashes the request's `Authorization` header (`Basic base64(user:password)`) with SHA-256 and compares the hash with the value stored under `auth` in a CloudFront KeyValueStore. A missing header, a wrong one, or an empty store gets a 401 with a `WWW-Authenticate` challenge, so the browser shows its login prompt.
- **Where the password lives.** Nowhere in AWS or git: `make ui-password` stores only the hash. Terraform never sees it, and the function code holds no secret. The hash is unsalted, so choose a long password (the script requires 12 characters or more).
- **The origins accept only this distribution.** The site bucket's policy lets only this distribution read it (OAC, `AWS:SourceArn`). The BFF's function URL requires IAM auth. CloudFront signs each request with SigV4 through a Lambda OAC, which replaces the browser's basic-auth header. Only this distribution may invoke the function, through `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction`. Direct calls to either origin get a 403.
- **The static user.** The BFF calls the APIs as its role, `ui-<env>-bff`.
  - **Producer API:** the role may invoke the seven producer routes above. The producer's `source_access` then decides which sources it may read or write, so list the role as a writer of the sources the UI should offer.
  - **Control API:** the role may only invoke the three GET routes above. No DELETE, PATCH, reindex or tables route is reachable, in the BFF's code or its IAM policy.
- **CSRF guard.** Browsers resend cached basic-auth credentials on cross-site requests. So every POST must carry `X-Requested-With: entingestion-ui`, which a cross-site form can't send, and when the browser sends `Sec-Fetch-Site` it must be `same-origin`. Otherwise the BFF returns 403.
- **Path ids** must match `^[A-Za-z0-9_-]{1,128}$`, otherwise 400. The query string is forwarded only on the two list routes.
- **Response headers.** On every response:
  - a CSP: `default-src 'self'`, with `connect-src` adding only the raw bucket's regional endpoint, `frame-ancestors 'none'` and `'wasm-unsafe-eval'` so the page can hash large files in WebAssembly;
  - HSTS for a year;
  - `nosniff`, `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`.
- **Logs.** The BFF logs the method, the route, the upstream status and the duration. It never logs request or response bodies.

## First-time setup

The producer stack reads the UI origin for its raw bucket's CORS, and the ui stack reads both APIs' URLs, so the first apply goes around once:

```bash
aws login --profile entingestion
services/producer/scripts/build_lambda.sh
services/ingestion/scripts/build_lambda.sh all
services/ui/bff/scripts/build_lambda.sh                     # -> services/ui/bff/dist/bff_lambda.zip
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
| `make -C terraform ui-dev ENV=<env>` | Runs Vite on `http://localhost:5173`, proxying `/api` to that environment's CloudFront with basic auth from `UI_USER` and `UI_PASSWORD`. The dev raw bucket's CORS also allows `http://localhost:5173` (`ui_extra_origins`) |

```bash
export UI_USER=ops UI_PASSWORD='…'       # the login set with ui-password
make -C terraform ui-dev ENV=dev
```

A BFF code change is a normal stack deploy: run `services/ui/bff/scripts/build_lambda.sh`, then `make deploy ENV=<env> STACK=ui`. It rolls back with `make rollback ENV=<env> FN=ui-<env>-bff VERSION=<n>`.

### End-to-end check

`services/ui/bff/scripts/ui_acceptance.py` drives the deployed UI's path end to end, covering:
- **the login gate:** 401s, and 403 on the direct function URL;
- **the security headers;**
- **the CSRF guard and the route allow-list;**
- **one batch of 3 small files:** ticket, S3 CORS preflight, presigned PUT, close, and polling until COMPLETE with 3 INDEXED;
- **cleanup:** it deletes the articles again, signed with your own AWS credentials.

```bash
cd services/ui/bff
UI_URL=$(make -sC ../../../terraform ui-url ENV=dev) UI_USER=… UI_PASSWORD='…' \
  AWS_REGION=us-west-2 uv run python scripts/ui_acceptance.py
```

## Limits

- **One shared login.** Everyone who knows it is the same user. There's no per-person audit: the producer records the BFF role as `created_by` of every process and upload.
- **Every source the BFF may write is open to everyone with the login.**
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
- **A search tab.**
