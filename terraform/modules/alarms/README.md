# alarms

An SNS topic encrypted with your KMS key, plus CloudWatch metric alarms defined as data. Every alarm notifies the topic when it goes into ALARM; set `notify_on_ok = true` to also notify when it recovers. Only CloudWatch in this account, and the account itself, can publish to the topic. You can subscribe email addresses to it.

Set `statistic` (Sum, Average, …) or `extended_statistic` (a percentile such as `p95`), not both. Or alarm on metric math: an `expression` over `metrics`, keyed by the ids it uses, each with its own `stat`:

```hcl
failure-ratio = {
  description = "More than 1% of runs failed permanently in the last hour"
  expression  = "IF(ok + failed >= 20, 100 * failed / (ok + failed), 0)"
  metrics = {
    ok     = { namespace = "Ingestion", metric_name = "Run_INDEXED", dimensions = { service = "ingestion" }, stat = "Sum" }
    failed = { namespace = "Ingestion", metric_name = "Run_FAILED_PERMANENT", dimensions = { service = "ingestion" }, stat = "Sum" }
  }
  threshold = 1
  period    = 3600
}
```

The topic's KMS key must let `cloudwatch.amazonaws.com` call `kms:Decrypt` and `kms:GenerateDataKey*`, or encrypted alarm notifications are dropped.

```hcl
module "alarms" {
  source      = "../../modules/alarms"
  topic_name  = "producer-dev-alarms"
  kms_key_arn = module.kms.key_arn
  alarms = {
    dlq-not-empty = {
      description = "Messages in the registrar DLQ"
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesVisible"
      dimensions  = { QueueName = module.queue.dlq_name }
      statistic   = "Maximum"
      threshold   = 1
    }
  }
}
```

| Output | Description |
|---|---|
| `topic_arn` | Alarm topic |
| `alarm_names` | Alarm names by key |
| `settings` | Alarm definitions, for plan assertions |
