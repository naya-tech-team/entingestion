#!/usr/bin/env bash
# Builds dist/bff_lambda.zip for the UI backend function ui-<env>-bff (python3.12, arm64).
# Deterministic: fixed file timestamps and no extra attributes, so an unchanged tree gives an
# unchanged zip and Terraform's source_code_hash only moves when the code does.
# Run before `terraform plan` (locally and in CI); Terraform never builds code.
set -euo pipefail

cd "$(dirname "$0")/.."

rm -rf build dist && mkdir -p build/lambda dist
# Install exactly the versions in uv.lock (what the tests ran against), runtime deps only, every
# downloaded file checked against the lock's hashes. botocore is bundled, not the runtime's copy.
uv export --frozen --no-dev --no-emit-project --quiet > build/requirements.txt
uv pip install --require-hashes -r build/requirements.txt --target build/lambda \
  --python-platform aarch64-manylinux2014 --python-version 3.12 --quiet
uv pip install . --no-deps --target build/lambda \
  --python-platform aarch64-manylinux2014 --python-version 3.12 --quiet
rm -rf build/lambda/bin build/lambda/.lock   # installer artefacts
# The project's own metadata embeds README.md; drop it so doc edits don't change the code hash.
rm -rf build/lambda/ui_bff-*.dist-info
find build/lambda -name "__pycache__" -type d -prune -exec rm -rf {} +
(cd build/lambda && find . -exec touch -t 202001010000 {} + && find . -type f | LC_ALL=C sort | zip -qX ../../dist/bff_lambda.zip -@)

echo "built $(pwd)/dist/bff_lambda.zip ($(du -h dist/bff_lambda.zip | cut -f1))"
