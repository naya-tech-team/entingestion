#!/usr/bin/env bash
# Builds dist/<group>.zip for the ingestion Lambdas (python3.12, arm64).
#
#   scripts/build_lambda.sh core   # dispatcher, claim, fetch, embed, validate, index, finalize, mark_failed, migrate, tables
#   scripts/build_lambda.sh docs   # extract, normalize, chunk
#   scripts/build_lambda.sh all
#
# Deterministic (same uv.lock + source -> same zip) so Terraform's source_code_hash only moves
# when code does. Run before `terraform plan`; Terraform never builds code (brief §8.5).
set -euo pipefail

cd "$(dirname "$0")/.."
LIMIT_MB=250  # Lambda's unzipped package limit
# Lambda's python3.12 runtime is Amazon Linux 2023 (glibc 2.34); psycopg-binary and tiktoken only
# publish aarch64 wheels for manylinux_2_28, so target that rather than manylinux2014.
PLATFORM=aarch64-manylinux_2_28

build() {
  local group="$1" out="build/$1"
  rm -rf "$out" "dist/$group.zip" && mkdir -p "$out/pkg" dist

  # Exactly the versions in uv.lock (what the tests ran against), runtime deps + this group;
  # every downloaded file is checked against the lock's hashes.
  uv export --frozen --no-dev --no-emit-project --quiet --extra "$group" > "$out/requirements.txt"
  uv pip install --require-hashes -r "$out/requirements.txt" --target "$out/pkg" \
    --python-platform "$PLATFORM" --python-version 3.12 --quiet
  uv pip install . --no-deps --target "$out/pkg" \
    --python-platform "$PLATFORM" --python-version 3.12 --quiet

  case "$group" in
    core)
      # migrate applies these; it connects to the writer endpoint with the RDS CA bundle.
      mkdir -p "$out/pkg/db" "$out/pkg/certs"
      cp -R db/migrations "$out/pkg/db/migrations"
      cp certs/rds-global-bundle.pem "$out/pkg/certs/"
      ;;
    docs)
      # No internet in Lambda: ship the tokenizer's encoding file (TIKTOKEN_CACHE_DIR=/var/task/tiktoken_cache).
      TIKTOKEN_CACHE_DIR="$PWD/$out/pkg/tiktoken_cache" uv run --quiet python -c \
        "import tiktoken; tiktoken.get_encoding('cl100k_base')"
      ;;
  esac

  # Installer artefacts, caches, and the project's own metadata (it embeds README.md, so doc
  # edits would otherwise change the code hash).
  rm -rf "$out/pkg/bin" "$out/pkg/.lock" "$out"/pkg/ingest-*.dist-info
  rm -rf "$out/pkg/pyarrow/tests" "$out/pkg/pyarrow/include"  # never loaded at runtime
  find "$out/pkg" -name "__pycache__" -type d -prune -exec rm -rf {} +

  local size_mb
  size_mb=$(du -sm "$out/pkg" | cut -f1)
  if (( size_mb > LIMIT_MB )); then
    echo "error: $group is ${size_mb} MB unzipped (limit ${LIMIT_MB} MB); use a container image" >&2
    exit 1
  fi

  (cd "$out/pkg" && find . -exec touch -t 202001010000 {} + && find . -type f | LC_ALL=C sort | zip -qX "../../../dist/$group.zip" -@)
  echo "built dist/$group.zip ($(du -h "dist/$group.zip" | cut -f1) zipped, ${size_mb} MB unzipped)"
}

# Note: `set -e` doesn't apply inside a function called from an && chain, so call build() as
# separate statements.
case "${1:-all}" in
  core | docs) build "$1" ;;
  all)
    build core
    build docs
    ;;
  *)
    echo "usage: $0 core|docs|all" >&2
    exit 2
    ;;
esac
