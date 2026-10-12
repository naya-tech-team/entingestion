#!/usr/bin/env bash
# Sets the upload UI's login (make ui-password ENV=<env>; docs/UI.md "Set or rotate the login").
#
# Prompts for a username and a password (not echoed) and stores only
# sha256("Basic " + base64(user:password)), lowercase hex, under the key "auth" in the UI's
# CloudFront KeyValueStore. The basic-auth function compares the hash of each request's
# Authorization header with it. The password never reaches git, Terraform state, the function
# code, a command line or the shell history. Takes effect at every edge within seconds; no apply.
#
#   scripts/ui_password.sh <key value store arn>
set -euo pipefail

kvs_arn="${1:?usage: ui_password.sh <key value store arn>}"

read -r -p "UI username: " user
if [[ -z "$user" || "$user" == *:* ]]; then
  echo "The username must not be empty or contain a colon." >&2
  exit 1
fi
read -r -s -p "UI password (at least 12 characters): " password
echo
read -r -s -p "Repeat the password: " again
echo
if [[ "$password" != "$again" ]]; then
  echo "The passwords differ; nothing changed." >&2
  exit 1
fi
if ((${#password} < 12)); then
  echo "Use at least 12 characters; nothing changed." >&2
  exit 1
fi

sha256_hex() {
  if command -v sha256sum >/dev/null; then sha256sum; else shasum -a 256; fi | cut -d' ' -f1
}

# printf is a shell builtin, so the password only travels through pipes, never argv.
token=$(printf '%s:%s' "$user" "$password" | base64 | tr -d '\n')
hash=$(printf 'Basic %s' "$token" | sha256_hex)
unset password again token

etag=$(aws cloudfront-keyvaluestore describe-key-value-store --kvs-arn "$kvs_arn" --query ETag --output text)
aws cloudfront-keyvaluestore put-key --kvs-arn "$kvs_arn" --key auth --value "$hash" \
  --if-match "$etag" --query ItemCount --output text >/dev/null

echo "Login set for user '$user'. Browsers ask for it again once their cached login fails."
