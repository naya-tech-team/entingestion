output "web_acl_arn" {
  description = "Web ACL ARN."
  value       = aws_wafv2_web_acl.this.arn
}

output "web_acl_name" {
  description = "Web ACL name (WAF metrics dimension)."
  value       = aws_wafv2_web_acl.this.name
}

output "log_group_name" {
  description = "WAF log group."
  value       = aws_cloudwatch_log_group.this.name
}

output "settings" {
  description = "Effective settings, for plan assertions in consuming stacks."
  value = {
    scope               = aws_wafv2_web_acl.this.scope
    managed_rule_groups = [for g in var.managed_rule_groups : g.name]
    count_rules         = { for g in var.managed_rule_groups : g.name => g.count_rules }
    rate_limit          = var.rate_limit
    associated          = keys(var.resource_arns)
    redacted_headers    = var.redacted_headers
  }
}
