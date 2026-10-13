#!/usr/bin/env bash
# dist/bff_lambda.zip for the UI backend function (the ui stack's bff_function_name).
exec "$(dirname "$0")/../../../../tools/build_lambda.sh" "$(dirname "$0")/.." bff_lambda.zip
