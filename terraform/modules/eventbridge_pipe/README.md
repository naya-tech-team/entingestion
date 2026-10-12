# eventbridge_pipe

An EventBridge Pipe from a **DynamoDB stream** to an **SQS queue**.

- **Filtering and failures:** the pipe drops records that don't match `filter_patterns`. A failing batch is bisected automatically and retried up to `max_retries` times, then its records go to `dlq_arn`.
- **IAM role:** built from the source and target ARNs alone, so it can only read that stream, send to that queue and DLQ, and write its own logs. Extra statements, such as `kms:Decrypt` on the source table's key, come in through `policy_documents`.
- **Logs:** written to an encrypted `/aws/vendedlogs/pipes/<name>` group.

```hcl
module "pipe" {
  source            = "../../modules/eventbridge_pipe"
  name              = "ingestion-dev-articles"
  source_stream_arn = data.aws_ssm_parameter.articles_stream_arn.value
  target_queue_arn  = module.ingest_queue.queue_arn
  dlq_arn           = module.pipe_dlq.queue_arn
  kms_key_arn       = module.kms.key_arn
  filter_patterns = [
    jsonencode({ eventName = ["INSERT", "MODIFY"], dynamodb = { NewImage = { status = { S = ["STAGED"] } } } }),
    jsonencode({ eventName = ["REMOVE"] }),
  ]
  policy_documents = [data.aws_iam_policy_document.producer_key.json]
}
```

| Output | Description |
|---|---|
| `pipe_arn`, `role_arn` | The pipe and its role |
| `log_group_name` | Pipe logs |
| `settings` | Effective settings, for plan assertions |
