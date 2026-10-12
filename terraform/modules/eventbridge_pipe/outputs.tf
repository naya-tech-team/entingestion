output "pipe_arn" {
  description = "Pipe ARN."
  value       = aws_pipes_pipe.this.arn
}

output "role_arn" {
  description = "Pipe execution role ARN."
  value       = aws_iam_role.this.arn
}

output "log_group_name" {
  description = "Pipe log group."
  value       = aws_cloudwatch_log_group.this.name
}

output "settings" {
  description = "Effective settings, for plan assertions in consuming stacks."
  value = {
    starting_position = var.starting_position
    max_retries       = var.max_retries
    filter_patterns   = var.filter_patterns
    dlq_arn           = var.dlq_arn
  }
}

output "role_permissions_boundaries" {
  description = "Permissions boundary of each IAM role the module created, for plan assertions in consuming stacks."
  value       = [aws_iam_role.this.permissions_boundary]
}
