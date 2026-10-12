output "queue_arn" {
  description = "Main queue ARN."
  value       = aws_sqs_queue.this.arn
}

output "queue_url" {
  description = "Main queue URL."
  value       = aws_sqs_queue.this.id
}

output "queue_name" {
  description = "Main queue name."
  value       = aws_sqs_queue.this.name
}

output "dlq_arn" {
  description = "Dead-letter queue ARN."
  value       = aws_sqs_queue.dlq.arn
}

output "dlq_url" {
  description = "Dead-letter queue URL."
  value       = aws_sqs_queue.dlq.id
}

output "dlq_name" {
  description = "Dead-letter queue name (for alarms)."
  value       = aws_sqs_queue.dlq.name
}

output "policy_id" {
  description = "Queue policy resource id. Depend on it before wiring producers (e.g. S3 notifications)."
  value       = aws_sqs_queue_policy.this.id
}

output "settings" {
  description = "Effective queue settings, for plan assertions in consuming stacks."
  value = {
    visibility_timeout_seconds    = aws_sqs_queue.this.visibility_timeout_seconds
    max_receive_count             = var.max_receive_count
    dlq_message_retention_seconds = aws_sqs_queue.dlq.message_retention_seconds
    sse_enabled                   = local.sse_sqs ? aws_sqs_queue.this.sqs_managed_sse_enabled && aws_sqs_queue.dlq.sqs_managed_sse_enabled : true
    kms_key_arn                   = var.kms_key_arn
  }
}
