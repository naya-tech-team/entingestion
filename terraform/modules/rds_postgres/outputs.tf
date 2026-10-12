output "instance_id" {
  description = "DB instance identifier."
  value       = aws_db_instance.this.identifier
}

output "arn" {
  description = "DB instance ARN (backup selections)."
  value       = aws_db_instance.this.arn
}

output "resource_id" {
  description = "Instance resource id (db-XXXX), used in rds-db:connect ARNs."
  value       = aws_db_instance.this.resource_id
}

output "endpoint" {
  description = "Hostname to connect to."
  value       = aws_db_instance.this.address
}

output "port" {
  description = "Port."
  value       = aws_db_instance.this.port
}

output "master_username" {
  description = "Master user."
  value       = aws_db_instance.this.username
}

output "master_secret_arn" {
  description = "Secrets Manager secret holding the master credentials."
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "security_group_id" {
  description = "Security group on the instance."
  value       = aws_security_group.this.id
}

output "connect_arn_prefix" {
  description = "Append a database user name to get the rds-db:connect resource ARN for IAM auth."
  value       = "arn:${data.aws_partition.current.partition}:rds-db:${split(":", aws_db_instance.this.arn)[3]}:${data.aws_caller_identity.current.account_id}:dbuser:${aws_db_instance.this.resource_id}/"
}

output "settings" {
  description = "Effective settings, for plan assertions in consuming stacks."
  value = {
    engine_version        = aws_db_instance.this.engine_version
    instance_class        = aws_db_instance.this.instance_class
    iam_auth              = aws_db_instance.this.iam_database_authentication_enabled
    storage_encrypted     = aws_db_instance.this.storage_encrypted
    publicly_accessible   = aws_db_instance.this.publicly_accessible
    backup_retention_days = aws_db_instance.this.backup_retention_period
    deletion_protection   = aws_db_instance.this.deletion_protection
    skip_final_snapshot   = aws_db_instance.this.skip_final_snapshot
  }
}

output "role_permissions_boundaries" {
  description = "Permissions boundary of each IAM role the module created, for plan assertions in consuming stacks."
  value       = aws_iam_role.monitoring[*].permissions_boundary
}
