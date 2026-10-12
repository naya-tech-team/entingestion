#!/usr/bin/env bash
# Builds dist/producer_lambda.zip for the api and registrar functions (python3.12, arm64).
# Deterministic: fixed file timestamps and no extra attributes, so an unchanged tree gives an
# unchanged zip and Terraform's source_code_hash only moves when the code does.
# Run before `terraform plan` (locally and in CI); Terraform never builds code.
set -euo pipefail

cd "$(dirname "$0")/.."

rm -rf build dist && mkdir -p build/lambda dist
# Install exactly the versions in uv.lock (what the tests ran against), runtime deps only, every
# downloaded file checked against the lock's hashes.
# boto3/botocore are bundled on purpose; the runtime's copy may lag (brief §6.1.6).
uv export --frozen --no-dev --no-emit-project --quiet > build/requirements.txt
uv pip install --require-hashes -r build/requirements.txt --target build/lambda \
  --python-platform aarch64-manylinux2014 --python-version 3.12 --quiet
uv pip install . --no-deps --target build/lambda \
  --python-platform aarch64-manylinux2014 --python-version 3.12 --quiet
rm -rf build/lambda/producer_client          # the CLI isn't needed in Lambda
rm -rf build/lambda/bin build/lambda/.lock   # installer artefacts
# The project's own metadata embeds README.md; drop it so doc edits don't change the code hash.
rm -rf build/lambda/producer-*.dist-info
find build/lambda -name "__pycache__" -type d -prune -exec rm -rf {} +
(cd build/lambda && find . -exec touch -t 202001010000 {} + && find . -type f | LC_ALL=C sort | zip -qX ../../dist/producer_lambda.zip -@)

echo "built $(pwd)/dist/producer_lambda.zip ($(du -h dist/producer_lambda.zip | cut -f1))"
