#!/usr/bin/env bash
# Post-deploy smoke test. Ingests tests/fixtures through the real API and S3, waits for the
# registrar to settle the process, then checks the counts and one article per file.
#
#   ENV=dev scripts/smoke.sh                 # API URL from the producer stack's Terraform output
#   PRODUCER_API_URL=https://... scripts/smoke.sh
#
# Environment:
#   PRODUCER_API_URL  the API's invoke URL; else `terraform output -raw api_url` of the producer
#                     stack in the ENV workspace (ENV is then required)
#   SOURCE            the source to write (default: smoke); your credentials must be its writer
#   TIMEOUT_SECONDS   how long to wait for the process to settle (default: 180)
#   AWS_REGION        only if the API URL carries no region (a custom domain)
set -euo pipefail

cd "$(dirname "$0")/.."
SOURCE="${SOURCE:-smoke}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-180}"

if [[ -z "${PRODUCER_API_URL:-}" ]]; then
  : "${ENV:?set ENV (the Terraform workspace) or PRODUCER_API_URL}"
  PRODUCER_API_URL=$(cd ../../terraform/stacks/producer && TF_DATA_DIR=".terraform/${ENV}" terraform output -raw api_url)
fi
export PRODUCER_API_URL SOURCE TIMEOUT_SECONDS

uv run python - <<'PY'
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from producer_client.client import ProducerClient
from producer_client.cli import iter_files

fixtures = Path("tests/fixtures")
files = iter_files(fixtures)
expected = {f.relative_to(fixtures).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
print(f"smoke: ingesting {len(files)} fixture file(s) via {os.environ['PRODUCER_API_URL']}")

run = subprocess.run(
    ["producer-cli", "ingest-dir", str(fixtures), "--name", "smoke test", "--source", os.environ["SOURCE"], "--close"],
    capture_output=True, text=True, check=False,
)
sys.stderr.write(run.stderr)
if run.returncode != 0:
    sys.exit(f"smoke: ingest-dir failed ({run.returncode}):\n{run.stdout}")
result = json.loads(run.stdout)
pid = result["process_id"]
assert result["process"]["expected_count"] == len(files), result["process"]

client = ProducerClient(os.environ["PRODUCER_API_URL"])
deadline = time.monotonic() + int(os.environ["TIMEOUT_SECONDS"])
while True:
    process = client.get_process(pid)
    if process["settled"]:
        break
    if time.monotonic() > deadline:
        sys.exit(f"smoke: {pid} not settled after {os.environ['TIMEOUT_SECONDS']}s: {process}")
    time.sleep(5)

failures = []
if process["registered_count"] != len(files):
    failures.append(f"registered_count {process['registered_count']} != {len(files)}")
if process["rejected_count"] != 0:
    failures.append(f"rejected_count {process['rejected_count']} != 0")

articles, token = [], None
while True:
    page = client.list_articles(pid, limit=100, next_token=token)
    articles += page["items"]
    token = page["next_token"]
    if not token:
        break
by_external_id = {a["external_id"]: a for a in articles}
if len(articles) != len(files):
    failures.append(f"{len(articles)} article rows != {len(files)} files")
for rel, sha in expected.items():
    a = by_external_id.get(rel)
    if a is None:
        failures.append(f"no article for {rel}")
    elif a["content_sha256"] != sha or a["status"] != "STAGED":
        failures.append(f"{rel}: sha256/status mismatch: {a['content_sha256']} {a['status']}")

if failures:
    sys.exit("smoke: FAILED\n  " + "\n  ".join(failures))
print(f"smoke: OK  process={pid} registered={process['registered_count']} articles={len(articles)}")
PY
