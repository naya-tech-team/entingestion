output "endpoint" {
  description = "Proxy endpoint (connect with an IAM auth token over TLS)."
  value       = aws_db_proxy.this.endpoint
}

output "proxy_arn" {
  description = "Proxy ARN."
  value       = aws_db_proxy.this.arn
}

output "proxy_resource_id" {
  description = "Proxy resource id (prx-XXXX), used in rds-db:connect ARNs for clients."
  value       = element(split(":", aws_db_proxy.this.arn), 6)
}

output "security_group_id" {
  description = "Security group on the proxy."
  value       = aws_security_group.this.id
}

output "connect_arn_prefix" {
  description = "Append a database user name to get the client rds-db:connect resource ARN."
  value       = "arn:${data.aws_partition.current.partition}:rds-db:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:dbuser:${element(split(":", aws_db_proxy.this.arn), 6)}/"
}

output "settings" {
  description = "Effective settings, for plan assertions in consuming stacks."
  value = {
    require_tls         = aws_db_proxy.this.require_tls
    default_auth_scheme = aws_db_proxy.this.default_auth_scheme
    engine_family       = aws_db_proxy.this.engine_family
  }
}

output "role_permissions_boundaries" {
  description = "Permissions boundary of each IAM role the module created, for plan assertions in consuming stacks."
  value       = [aws_iam_role.this.permissions_boundary]
}
