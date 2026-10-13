# entingestion-common

Code shared by the producer, ingestion and UI backend (BFF) services. Its only runtime dependency is botocore.

| Module | Contents |
|---|---|
| `entcommon.authz` | Caller identities (an assumed-role session also matches its role), shell-style ARN patterns, `expand_principal`, and the per-source `AccessPolicy` (writers, readers, tenant, PII policy, admins). Fails closed. |
| `entcommon.paging` | `next_token` encoding: unpadded urlsafe base64 of compact, sorted JSON. Decoding checks the key shape and an optional binding. |
| `entcommon.dynamo` | `plain()` (Decimals to int/float), `is_condition_failure()`, `cancellation_codes()`. |
| `entcommon.sigv4` | SigV4 headers for `execute-api` calls, and the region taken from an execute-api URL in any partition. |
| `entcommon.timeutil` | UTC `utcnow`, `iso` / `iso_now` (milliseconds, `Z`), `parse_iso`. |

Each service depends on it by path (`[tool.uv.sources] entingestion-common = { path = "../common", editable = true }`). The Lambda builds install it with `--no-deps` next to the hash-locked requirements.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
uv run python ../../tools/check_licenses.py
```
