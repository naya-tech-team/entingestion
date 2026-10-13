# UI backend (BFF)

The Lambda behind CloudFront's `/api/*` for the upload UI ([docs/UI.md](../../../docs/UI.md)). It's the UI's "static user". It forwards a fixed allow-list of routes to the producer and control APIs, signing each request with SigV4 as its own role (the `ui` stack's `bff_role_arn` output), and returns the upstream status and JSON body unchanged.

- **Allow-list:** the seventeen routes in [`src/bff/app.py`](src/bff/app.py), listed below. Every other method or path gets a 404. Path ids must match `^[A-Za-z0-9_-]{1,128}$`, otherwise 400.
- **CSRF guard:** every POST needs `X-Requested-With: entingestion-ui`. If `Sec-Fetch-Site` is present it must be `same-origin`. Otherwise the request gets a 403.
- **Operations guard rails:** on the three actions (`/api/ops/ingest`, `/api/ops/processes/{id}/ingest`, `/api/ops/dlq/redrive`) the body must be a JSON object (malformed JSON gets 400 `{"message": "invalid JSON"}`). `/api/ops/ingest` takes at most `MAX_RERUN_IDS` (100) `article_ids` and no `from_stage`; `/api/ops/dlq/redrive` takes a `max` of at most `MAX_REDRIVE` (200). Anything over gets 422 in the APIs' validation shape, `{"message": "request validation failed", "errors": [{"loc": [...], "msg": ...}]}`, without calling upstream.
- **Forwarded headers:** only `Idempotency-Key`, on POSTs. The query string is forwarded only on the list routes (`/api/processes`, `/api/processes/{id}/uploads`, `/api/ops/articles`, `/api/ops/tables`).
- **Upstream calls:** connect timeout `CONNECT_TIMEOUT` (3 s), read timeout `READ_TIMEOUT` (25 s; CloudFront gives the origin 30 s), no retries. Each is signed for `execute-api` in the region of the API's host (else the function's `AWS_REGION`). A network error or timeout returns 502 `{"message": "upstream unavailable"}`.
- **Logs:** method, route, upstream status and duration. Each Operations action also logs its route, path ids, `dry_run`, the number of `article_ids` and the status. Request and response bodies are never logged.

| BFF route | Upstream |
|---|---|
| GET `/api/sources` | producer GET `/v1/sources` |
| GET `/api/processes` | producer GET `/v1/processes` |
| POST `/api/processes` | producer POST `/v1/processes` |
| GET `/api/processes/{id}` | producer GET `/v1/processes/{id}` |
| POST `/api/processes/{id}/close` | producer POST `/v1/processes/{id}/close` |
| POST `/api/processes/{id}/uploads` | producer POST `/v1/processes/{id}/uploads` |
| GET `/api/processes/{id}/uploads` | producer GET `/v1/processes/{id}/uploads` |
| GET `/api/processes/{id}/summary` | control GET `/v1/processes/{id}/summary` |
| GET `/api/processes/{id}/articles` | control GET `/v1/processes/{id}/articles` |
| GET `/api/articles/{id}/status` | control GET `/v1/articles/{id}/status` |
| GET `/api/ops/overview` | control GET `/v1/ops/overview` |
| GET `/api/ops/articles` | control GET `/v1/articles` |
| GET `/api/ops/tables` | control GET `/v1/tables` |
| POST `/api/ops/search` | control POST `/v1/search` |
| POST `/api/ops/ingest` | control POST `/v1/ingest` (capped) |
| POST `/api/ops/processes/{id}/ingest` | control POST `/v1/processes/{id}/ingest` |
| POST `/api/ops/dlq/redrive` | control POST `/v1/dlq/redrive` (capped) |

Reindex, table switches and retirements, and every DELETE and PATCH stay CLI-only: they aren't routed here, and the role may not invoke them.

## Configuration

Every environment variable is read in one place, [`src/bff/config.py`](src/bff/config.py) (`Settings`). The handler builds the settings on its first invocation, so a function deployed without either URL fails at once instead of on a later route.

| Variable | Default |
|---|---|
| `PRODUCER_API_URL` | required: the producer API's stage invoke URL (the `ui` stack reads it from SSM) |
| `CONTROL_API_URL` | required: the control API's stage invoke URL (likewise) |
| `MAX_RERUN_IDS` | `100` |
| `MAX_REDRIVE` | `200` |
| `CONNECT_TIMEOUT` | `3` (seconds) |
| `READ_TIMEOUT` | `25` (seconds) |

Shared code (SigV4 signing) comes from [`services/common`](../../common) (`entcommon`).

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
uv run python ../../../tools/check_licenses.py
```

The tests run against local HTTP servers that stand in for the two APIs, so they need no AWS access.

## Acceptance test (after deploying)

```bash
UI_ENV=dev UI_USER=... UI_PASSWORD='...' uv run python scripts/ui_acceptance.py
```

It drives the deployed UI end to end (see its docstring). The UI URL, the BFF function and the producer API URL come from `UI_URL`, `UI_BFF_FUNCTION` and `PRODUCER_API_URL`, or else from the `ui` and `producer` stacks' Terraform outputs (`url`, `bff_function_name`, `api_url`) in the `UI_ENV` workspace. `UI_SOURCE` (default `acceptance`) is the source the batch writes.

## Build

```bash
./scripts/build_lambda.sh      # -> dist/bff_lambda.zip (from uv.lock, linux/arm64)
make -C ../../../terraform plan ENV=dev STACK=ui
```
