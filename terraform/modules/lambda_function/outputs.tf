output "function_name" {
  description = "Function name."
  value       = aws_lambda_function.this.function_name
}

output "function_arn" {
  description = "Function ARN."
  value       = aws_lambda_function.this.arn
}

output "invoke_arn" {
  description = "Unqualified API Gateway integration ARN ($LATEST). Prefer alias_invoke_arn."
  value       = aws_lambda_function.this.invoke_arn
}

output "version" {
  description = "Version this apply published (the alias points at it)."
  value       = aws_lambda_function.this.version
}

output "alias_name" {
  description = "Name of the alias callers invoke."
  value       = aws_lambda_alias.live.name
}

output "alias_arn" {
  description = "Qualified ARN (function:alias) for state machines, event source mappings, schedules and IAM grants."
  value       = aws_lambda_alias.live.arn
}

output "alias_invoke_arn" {
  description = "Qualified API Gateway integration ARN (the alias)."
  value       = aws_lambda_alias.live.invoke_arn
}

output "role_arn" {
  description = "Execution role ARN."
  value       = aws_iam_role.this.arn
}

output "role_name" {
  description = "Execution role name."
  value       = aws_iam_role.this.name
}

output "log_group_name" {
  description = "Log group name."
  value       = aws_cloudwatch_log_group.this.name
}

output "settings" {
  description = "Effective function settings, for plan assertions in consuming stacks."
  value = {
    handler       = aws_lambda_function.this.handler
    runtime       = aws_lambda_function.this.runtime
    architectures = aws_lambda_function.this.architectures
    memory_size   = aws_lambda_function.this.memory_size
    timeout       = aws_lambda_function.this.timeout
    tracing_mode  = aws_lambda_function.this.tracing_config[0].mode
    package_type  = aws_lambda_function.this.package_type
    in_vpc        = var.vpc_config != null
    environment   = var.environment
    log_retention = aws_cloudwatch_log_group.this.retention_in_days
    publish       = aws_lambda_function.this.publish
    reserved      = aws_lambda_function.this.reserved_concurrent_executions
  }
}

output "role_permissions_boundaries" {
  description = "Permissions boundary of each IAM role the module created, for plan assertions in consuming stacks."
  value       = [aws_iam_role.this.permissions_boundary]
}
