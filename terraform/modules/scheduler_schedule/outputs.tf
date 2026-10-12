output "schedule_arn" {
  description = "Schedule ARN."
  value       = aws_scheduler_schedule.this.arn
}

output "role_arn" {
  description = "Role the scheduler assumes to call the target."
  value       = aws_iam_role.this.arn
}

output "settings" {
  description = "Effective settings, for plan assertions in consuming stacks."
  value = {
    schedule_expression = var.schedule_expression
    state               = aws_scheduler_schedule.this.state
    target_arn          = var.target_arn
    target_actions      = var.target_actions
    retry_attempts      = var.retry_attempts
  }
}

output "role_permissions_boundaries" {
  description = "Permissions boundary of each IAM role the module created, for plan assertions in consuming stacks."
  value       = [aws_iam_role.this.permissions_boundary]
}
