# Alarms -> SNS topic (KMS) -> email subscriptions (required in prod), on ALARM only.

module "alarms" {
  source = "../../modules/alarms"

  topic_name      = "${local.name_prefix}-alarms"
  kms_key_arn     = module.kms.key_arn
  email_endpoints = var.alarm_emails
  notify_on_ok    = var.alarm_notify_on_ok

  alarms = {
    # Messages the redrive policy moves into a DLQ don't count in its NumberOfMessagesSent, so a
    # new arrival is an increase of the visible count (or a direct send).
    registrar-dlq-new-messages = {
      description = "New messages in the registrar DLQ (failed 5 times). See the runbook: DLQ redrive."
      expression  = "MAX([DIFF(visible), sent])"
      metrics = {
        visible = { namespace = "AWS/SQS", metric_name = "ApproximateNumberOfMessagesVisible", dimensions = { QueueName = module.registrar_queue.dlq_name }, stat = "Maximum" }
        sent    = { namespace = "AWS/SQS", metric_name = "NumberOfMessagesSent", dimensions = { QueueName = module.registrar_queue.dlq_name }, stat = "Sum" }
      }
      threshold = 1
    }
    registrar-dlq-not-drained = {
      description         = "The registrar DLQ has held messages for 24 hours. See the runbook: DLQ redrive."
      namespace           = "AWS/SQS"
      metric_name         = "ApproximateNumberOfMessagesVisible"
      dimensions          = { QueueName = module.registrar_queue.dlq_name }
      statistic           = "Minimum"
      threshold           = 1
      period              = 3600
      evaluation_periods  = 24
      datapoints_to_alarm = 24
    }
    registrar-queue-backlog = {
      description = "The oldest upload event has waited over ${var.registrar_queue_age_alarm_seconds} s for the registrar: it's failing, throttled or at its reserved concurrency."
      namespace   = "AWS/SQS"
      metric_name = "ApproximateAgeOfOldestMessage"
      dimensions  = { QueueName = module.registrar_queue.queue_name }
      statistic   = "Maximum"
      threshold   = var.registrar_queue_age_alarm_seconds
    }
    registrar-errors = {
      description         = "Registrar Lambda errors for 15 minutes (3 x 5 min)."
      namespace           = "AWS/Lambda"
      metric_name         = "Errors"
      dimensions          = { FunctionName = module.registrar_function.function_name }
      statistic           = "Sum"
      threshold           = 1
      evaluation_periods  = 3
      datapoints_to_alarm = 3
    }
    api-5xx = {
      description = "API returned 5 or more 5XX responses in 5 minutes."
      namespace   = "AWS/ApiGateway"
      metric_name = "5XXError"
      dimensions  = { ApiName = local.name_prefix, Stage = module.api.stage_name }
      statistic   = "Sum"
      threshold   = 5
    }
    unknown-uploads = {
      description = "Objects arrived for uploads the registrar doesn't know (lost tickets, or a redrive older than the upload rows). See the runbook."
      namespace   = "Producer"
      metric_name = "UnknownUpload"
      dimensions  = { service = "producer" }
      statistic   = "Sum"
      threshold   = 1
    }
    janitor-errors = {
      description         = "Janitor errors in 2 consecutive hourly runs: tickets and idle processes aren't being settled."
      namespace           = "AWS/Lambda"
      metric_name         = "Errors"
      dimensions          = { FunctionName = module.janitor_function.function_name }
      statistic           = "Sum"
      threshold           = 1
      period              = 3600
      evaluation_periods  = 2
      datapoints_to_alarm = 2
    }
    uploads-rejected = {
      description = "An upload failed verification (size/SHA-256 mismatch). See the runbook: rejected uploads."
      namespace   = "Producer"
      metric_name = "Upload_rejected"
      dimensions  = { service = "producer" }
      statistic   = "Sum"
      threshold   = 1
    }
  }
}
