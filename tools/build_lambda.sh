#!/usr/bin/env bash
# Builds <service dir>/dist/<zip name> for a single-package Python Lambda (python3.12, arm64):
# the producer and the UI backend (services/*/scripts/build_lambda.sh call this).
#
#   tools/build_lambda.sh <service dir> <zip name> [path to drop, relative to build/lambda ...]
#
# Deterministic: fixed file timestamps and no extra attributes, so an unchanged tree gives an
# unchanged zip and Terraform's source_code_hash only moves when the code does.
# Run before `terraform plan`; Terraform never builds code.
set -euo pipefail

if (($# < 2)); then
  echo "usage: $0 <service dir> <zip name> [path to drop, relative to build/lambda ...]" >&2
  exit 2
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMMON="$ROOT/services/common"
SERVICE="$1" ZIP="$2"
shift 2

cd "$SERVICE"
# The project's distribution name as installed (PEP 503 normalized, then - and . become _).
PROJECT=$(sed -n 's/^name = "\(.*\)"/\1/p' pyproject.toml | head -n1 | tr 'A-Z' 'a-z' | tr '.-' '__')
PLATFORM=(--python-platform aarch64-manylinux2014 --python-version 3.12)

rm -rf build dist && mkdir -p build/lambda dist
# Install exactly the versions in uv.lock (what the tests ran against), runtime deps only, every
# downloaded file checked against the lock's hashes. Local path dependencies (services/common)
# have no hash, so they're left out of the export and installed below without dependencies.
# botocore is bundled on purpose; the runtime's copy may lag.
uv export --frozen --no-dev --no-emit-project --no-emit-local --quiet > build/requirements.txt
uv pip install --require-hashes -r build/requirements.txt --target build/lambda "${PLATFORM[@]}" --quiet
uv pip install "$COMMON" --no-deps --target build/lambda "${PLATFORM[@]}" --quiet
uv pip install . --no-deps --target build/lambda "${PLATFORM[@]}" --quiet

for path in "$@"; do
  rm -rf "build/lambda/$path"
done
rm -rf build/lambda/bin build/lambda/.lock # installer artefacts
# Our projects' metadata embeds their README.md; drop it so doc edits don't change the code hash.
rm -rf build/lambda/"$PROJECT"-*.dist-info build/lambda/entingestion_common-*.dist-info
find build/lambda -name "__pycache__" -type d -prune -exec rm -rf {} +
(cd build/lambda && find . -exec touch -t 202001010000 {} + && find . -type f | LC_ALL=C sort | zip -qX "../../dist/$ZIP" -@)

echo "built $(pwd)/dist/$ZIP ($(du -h "dist/$ZIP" | cut -f1))"
