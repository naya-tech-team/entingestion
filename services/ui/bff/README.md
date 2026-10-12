# UI backend (BFF)

The Lambda behind CloudFront's `/api/*` for the upload UI ([docs/UI.md](../../../docs/UI.md)). It's the UI's "static user". It forwards a fixed allow-list of routes to the producer and control APIs, signing each request with SigV4 as its own role (`ui-<env>-bff`), and returns the upstream status and JSON body unchanged.

- **Allow-list:** the ten routes in [`src/bff/app.py`](src/bff/app.py). Every other method or path gets a 404. Path ids must match `^[A-Za-z0-9_-]{1,128}$`, otherwise 400.
- **CSRF guard:** every POST needs `X-Requested-With: entingestion-ui`. If `Sec-Fetch-Site` is present it must be `same-origin`. Otherwise the request gets a 403.
- **Forwarded headers:** only `Idempotency-Key`, on POSTs. The query string is forwarded only on the two list routes.
- **Upstream calls:** connect timeout 3 s, read timeout 25 s, no retries. A network error or timeout returns 502 `{"message": "upstream unavailable"}`.
- **Logs:** method, route, upstream status and duration. Request and response bodies are never logged.
- **Environment:** `PRODUCER_API_URL` and `CONTROL_API_URL`, the stage invoke URLs. The `ui` stack reads them from SSM.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
uv run python ../../../tools/check_licenses.py
```

The tests run against local HTTP servers that stand in for the two APIs, so they need no AWS access.

## Build

```bash
./scripts/build_lambda.sh      # -> dist/bff_lambda.zip (from uv.lock, linux/arm64)
make -C ../../../terraform plan ENV=dev STACK=ui
```
