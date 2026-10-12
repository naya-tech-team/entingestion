# lambda_function

A Lambda function with its execution role and log group.

These are always on:
- The log group is created before the function, with retention and KMS encryption.
- Active X-Ray tracing.
- KMS encryption of environment variables.
- The role can write only to the function's own log group, plus X-Ray. The function's own permissions come from `policy_json`.
- Every code or configuration change publishes a version, and the `live` alias (`alias_name`) points at the version the apply published. Callers use `alias_arn` / `alias_invoke_arn`, so a bad deploy rolls back by moving the alias to the previous version (`make rollback`); the next apply moves it forward again.

Code comes from one of:
- a local zip (`code.zip_path`),
- a zip in S3 (`code.s3_bucket` + `code.s3_key`),
- a container image (`code.image_uri`).

Both zip modes need `code.source_code_hash`. With an image, `handler` becomes the image command, so one image can serve several functions.

Optional inputs:
- `vpc_config`: runs the function in private subnets and attaches `AWSLambdaVPCAccessExecutionRole` for the ENIs.
- `layers`: zip packages only.
- `reserved_concurrent_executions`: caps the function, for example to stay within a downstream quota.
- `permissions_boundary`: a policy ARN set as the execution role's permissions boundary. The stacks pass the workload boundary from `terraform/bootstrap`; the other role-creating modules take the same input.

```hcl
module "api_fn" {
  source      = "../../modules/lambda_function"
  name        = "producer-dev-api"
  handler     = "producer.api.lambda_handler"
  memory_size = 512
  timeout     = 29
  code        = { zip_path = var.zip, source_code_hash = filebase64sha256(var.zip) }
  policy_json = data.aws_iam_policy_document.api.json
  kms_key_arn = module.kms.key_arn
}
```

| Output | Description |
|---|---|
| `function_name`, `function_arn`, `invoke_arn` | The function (unqualified: `$LATEST`) |
| `alias_arn`, `alias_invoke_arn`, `alias_name`, `version` | The `live` alias callers invoke, and the version it points at |
| `role_arn`, `role_name` | Execution role |
| `log_group_name` | `/aws/lambda/<name>` |
| `settings` | Effective settings (handler, runtime, package type, VPC, ...), for plan assertions |

A runnable example is in [`examples/basic`](examples/basic).
