# sqs_queue

An SQS queue with its dead-letter queue (`<name>-dlq`). Both queues use SQS-managed encryption (SSE-SQS), or the customer managed key in `kms_key_arn`, and both have a queue policy that refuses requests without TLS. After `max_receive_count` failed receives, a message moves to the DLQ, and only this queue may redrive into that DLQ. Extra resource policy, for example letting an S3 bucket send messages, is merged into the main queue's policy through `policy_documents`.

With `kms_key_arn`, everything that sends or receives needs that key: IAM roles through their policies, AWS services that send (S3 notifications, SNS, EventBridge) through the key policy.

```hcl
module "queue" {
  source                     = "../../modules/sqs_queue"
  name                       = "producer-dev-registrar"
  visibility_timeout_seconds = 1800 # 6x a 300 s Lambda timeout
  policy_documents           = [data.aws_iam_policy_document.s3_send.json]
}
```

When another resource sends to the queue (such as `aws_s3_bucket_notification`), make it depend on the `policy_id` output. AWS validates the destination's permissions when the notification is created.

| Output | Description |
|---|---|
| `queue_arn`, `queue_url`, `queue_name` | Main queue |
| `dlq_arn`, `dlq_url`, `dlq_name` | Dead-letter queue |
| `policy_id` | Queue policy, to depend on |
| `settings` | Effective settings, for plan assertions |
