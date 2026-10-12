output "cluster_id" {
  description = "Cluster identifier."
  value       = aws_rds_cluster.this.id
}

output "cluster_arn" {
  description = "Cluster ARN."
  value       = aws_rds_cluster.this.arn
}

output "cluster_resource_id" {
  description = "Cluster resource id (cluster-XXXX), used in rds-db:connect ARNs."
  value       = aws_rds_cluster.this.cluster_resource_id
}

output "endpoint" {
  description = "Writer endpoint."
  value       = aws_rds_cluster.this.endpoint
}

output "reader_endpoint" {
  description = "Reader endpoint."
  value       = aws_rds_cluster.this.reader_endpoint
}

output "port" {
  description = "Port."
  value       = aws_rds_cluster.this.port
}

output "database_name" {
  description = "Initial database."
  value       = aws_rds_cluster.this.database_name
}

output "master_username" {
  description = "Master user."
  value       = aws_rds_cluster.this.master_username
}

output "master_secret_arn" {
  description = "Secrets Manager secret holding the master credentials."
  value       = aws_rds_cluster.this.master_user_secret[0].secret_arn
}

output "security_group_id" {
  description = "Security group on the cluster."
  value       = aws_security_group.this.id
}

output "settings" {
  description = "Effective settings, for plan assertions in consuming stacks."
  value = {
    engine_version      = aws_rds_cluster.this.engine_version
    min_acu             = var.min_acu
    max_acu             = var.max_acu
    instance_count      = var.instance_count
    iam_auth            = aws_rds_cluster.this.iam_database_authentication_enabled
    storage_encrypted   = aws_rds_cluster.this.storage_encrypted
    deletion_protection = aws_rds_cluster.this.deletion_protection
    skip_final_snapshot = aws_rds_cluster.this.skip_final_snapshot
  }
}

output "role_permissions_boundaries" {
  description = "Permissions boundary of each IAM role the module created, for plan assertions in consuming stacks."
  value       = aws_iam_role.monitoring[*].permissions_boundary
}
