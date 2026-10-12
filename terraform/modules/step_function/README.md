# step_function

A Step Functions state machine (STANDARD by default) with:
- logging to an encrypted `/aws/vendedlogs/states/<name>` group (execution data excluded unless `include_execution_data`),
- X-Ray tracing,
- execution data encrypted with your KMS key.

The role gets only log delivery, X-Ray and the key (scoped by encryption context to this state machine). Pass what the workflow invokes through `policy_documents`. Render the definition with `templatefile()`.

Because execution data is encrypted with a customer key, anyone who calls `StartExecution` also needs `kms:GenerateDataKey` on that key.

```hcl
module "ingest" {
  source           = "../../modules/step_function"
  name             = "ingestion-dev-ingest"
  definition       = templatefile("${path.module}/asl/ingest.asl.json.tftpl", { claim_arn = module.claim.function_arn })
  policy_documents = [data.aws_iam_policy_document.invoke_steps.json]
  kms_key_arn      = module.kms.key_arn
}
```

| Output | Description |
|---|---|
| `state_machine_arn`, `state_machine_name` | The workflow |
| `role_arn` | Execution role |
| `log_group_name` | Execution logs |
| `settings` | Effective settings, for plan assertions |
