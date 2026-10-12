# lambda_sqs_trigger

Connects an SQS queue to a Lambda function. Partial batch responses (`ReportBatchItemFailures`) are always on, so a failed message is retried on its own and the rest of the batch isn't. The function has to return `{"batchItemFailures": [...]}`, for example with Powertools' `process_partial_response`.

`max_concurrency` caps how many concurrent invocations this queue can drive. It's useful for low-priority queues (backfill) or for downstream quotas such as Bedrock.

```hcl
module "ingest_trigger" {
  source                  = "../../modules/lambda_sqs_trigger"
  function_name           = module.dispatcher.function_name
  queue_arn               = module.ingest_queue.queue_arn
  batch_size              = 10
  batching_window_seconds = 5
  max_concurrency         = 5
}
```

The function's role needs `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` and `sqs:ChangeMessageVisibility` on the queue. The caller grants them, because modules never invent permissions.

| Output | Description |
|---|---|
| `mapping_uuid` | Event source mapping |
| `settings` | Effective settings, for plan assertions |
