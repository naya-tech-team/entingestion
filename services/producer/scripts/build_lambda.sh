#!/usr/bin/env bash
# dist/producer_lambda.zip for the api and registrar functions, without the CLI (producer_client).
exec "$(dirname "$0")/../../../tools/build_lambda.sh" "$(dirname "$0")/.." producer_lambda.zip producer_client
