resource "aws_lambda_event_source_mapping" "this" {
  event_source_arn                   = var.queue_arn
  function_name                      = var.function_name
  batch_size                         = var.batch_size
  maximum_batching_window_in_seconds = var.batching_window_seconds
  enabled                            = var.enabled

  # Always: a failed message is retried alone, not the whole batch. The function must return
  # {"batchItemFailures": [...]} (e.g. Powertools process_partial_response).
  function_response_types = ["ReportBatchItemFailures"]

  dynamic "scaling_config" {
    for_each = var.max_concurrency == null ? [] : [var.max_concurrency]

    content {
      maximum_concurrency = scaling_config.value
    }
  }
}
