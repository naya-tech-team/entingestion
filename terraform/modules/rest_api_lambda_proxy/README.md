# rest_api_lambda_proxy

A regional REST API that sends every request to one Lambda function (`AWS_PROXY`). It covers both the root `ANY` method and `{proxy+}` `ANY`, and both require `AWS_IAM` (SigV4) auth.

It includes:
- A redeploying deployment, plus a stage with JSON access logs and X-Ray.
- Execution logging, metrics and throttling on every method.
- Pre-created access and execution log groups, with retention and KMS encryption.
- The Lambda invoke permission.
- An IAM policy that grants callers `execute-api:Invoke`.

`manage_apigw_account` creates the account-level CloudWatch Logs role for API Gateway. This is one setting per account and region, so enable it in at most one stack, and only when the account has no role configured.

```hcl
module "api" {
  source                = "../../modules/rest_api_lambda_proxy"
  name                  = "producer-dev"
  lambda_function_name  = module.api_fn.function_name
  lambda_invoke_arn     = module.api_fn.alias_invoke_arn # deploys roll back by moving the alias
  lambda_qualifier      = module.api_fn.alias_name
  log_kms_key_arn       = module.kms.key_arn
  invoke_policy_name    = "producer-api-invoke-dev"
  invoke_resource_paths = ["*/v1/*"]
}
```

| Output | Description |
|---|---|
| `invoke_url` | Stage URL |
| `invoke_policy_arn` | Attach to callers |
| `execution_arn`, `rest_api_id`, `stage_name`, `stage_arn` | Wiring (WAF uses `stage_arn`) |
| `settings` | Effective settings, for plan assertions |
