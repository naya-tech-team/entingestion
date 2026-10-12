#!/usr/bin/env bash
# Print short-lived credentials for an AWS profile as shell exports, for tools whose SDK can't read
# an `aws login` session or a role chain by itself (docs/AWS_ACCESS.md):
#
#   eval "$(tools/aws-env.sh entingestion-dev)"     # then run the tool in the same shell
#
# The AWS CLI resolves the profile (login session -> deploy role). The credentials expire within
# the role's session limit (1 hour); run it again when they do. Nothing is written to disk.
set -euo pipefail
profile="${1:-${AWS_PROFILE:-}}"
if [[ -z "$profile" ]]; then
  echo "usage: eval \"\$($0 <profile>)\"" >&2
  exit 2
fi
aws configure export-credentials --profile "$profile" --format env
echo "export AWS_REGION=$(aws configure get region --profile "$profile" || echo us-west-2)"
