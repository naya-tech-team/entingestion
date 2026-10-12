output "rest_api_id" {
  description = "REST API id."
  value       = aws_api_gateway_rest_api.this.id
}

output "execution_arn" {
  description = "Execution ARN (for IAM and Lambda permissions)."
  value       = aws_api_gateway_rest_api.this.execution_arn
}

output "stage_name" {
  description = "Stage name."
  value       = aws_api_gateway_stage.this.stage_name
}

output "stage_arn" {
  description = "Stage ARN (for WAF associations)."
  value       = aws_api_gateway_stage.this.arn
}

output "invoke_url" {
  description = "Stage invoke URL."
  value       = aws_api_gateway_stage.this.invoke_url
}

output "invoke_policy_arn" {
  description = "IAM policy granting execute-api:Invoke; attach it to callers."
  value       = aws_iam_policy.invoke.arn
}

output "access_log_group_name" {
  description = "Access log group."
  value       = aws_cloudwatch_log_group.access.name
}

output "settings" {
  description = "Effective API settings, for plan assertions in consuming stacks."
  value = {
    endpoint_types       = aws_api_gateway_rest_api.this.endpoint_configuration[0].types
    root_authorization   = aws_api_gateway_method.root.authorization
    proxy_authorization  = aws_api_gateway_method.proxy.authorization
    integration_type     = aws_api_gateway_integration.proxy.type
    xray_tracing_enabled = aws_api_gateway_stage.this.xray_tracing_enabled
    throttle_rate_limit  = var.throttle_rate_limit
    throttle_burst_limit = var.throttle_burst_limit
    lambda_qualifier     = aws_lambda_permission.apigw.qualifier
  }
}

output "role_permissions_boundaries" {
  description = "Permissions boundary of each IAM role the module created, for plan assertions in consuming stacks."
  value       = aws_iam_role.apigw_cloudwatch[*].permissions_boundary
}
