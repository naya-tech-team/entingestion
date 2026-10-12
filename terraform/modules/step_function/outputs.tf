output "state_machine_arn" {
  description = "State machine ARN."
  value       = aws_sfn_state_machine.this.arn
}

output "state_machine_name" {
  description = "State machine name."
  value       = aws_sfn_state_machine.this.name
}

output "role_arn" {
  description = "Execution role ARN."
  value       = aws_iam_role.this.arn
}

output "log_group_name" {
  description = "Execution log group."
  value       = aws_cloudwatch_log_group.this.name
}

output "settings" {
  description = "Effective settings, for plan assertions in consuming stacks."
  value = {
    type              = aws_sfn_state_machine.this.type
    log_level         = var.log_level
    tracing_enabled   = true
    encryption_type   = "CUSTOMER_MANAGED_KMS_KEY"
    definition_start  = jsondecode(var.definition).StartAt
    definition_states = keys(jsondecode(var.definition).States)
  }
}

output "role_permissions_boundaries" {
  description = "Permissions boundary of each IAM role the module created, for plan assertions in consuming stacks."
  value       = [aws_iam_role.this.permissions_boundary]
}
