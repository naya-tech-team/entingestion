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
# The region: the profile's, else AWS_REGION / AWS_DEFAULT_REGION from this shell. No default:
# a guessed region would send calls to the wrong place.
region=$(aws configure get region --profile "$profile" 2>/dev/null || true)
region="${region:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
if [[ -z "$region" ]]; then
  echo "$0: no region for profile $profile: set region in its ~/.aws/config section, or export AWS_REGION" >&2
  exit 1
fi
aws configure export-credentials --profile "$profile" --format env
# Both names: the AWS CLI and most SDKs read AWS_REGION, but boto3 (the Python scripts,
# teardown.py) reads only AWS_DEFAULT_REGION and otherwise falls back to the default profile's.
echo "export AWS_REGION=$region"
echo "export AWS_DEFAULT_REGION=$region"
