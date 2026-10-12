output "mapping_uuid" {
  description = "Event source mapping UUID."
  value       = aws_lambda_event_source_mapping.this.uuid
}

output "settings" {
  description = "Effective settings, for plan assertions in consuming stacks."
  value = {
    function_name           = var.function_name
    batch_size              = var.batch_size
    batching_window_seconds = var.batching_window_seconds
    max_concurrency         = var.max_concurrency
    function_response_types = aws_lambda_event_source_mapping.this.function_response_types
  }
}
