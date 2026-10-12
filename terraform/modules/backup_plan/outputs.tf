output "vault_arn" {
  description = "Vault in this region."
  value       = aws_backup_vault.this.arn
}

output "copy_vault_arn" {
  description = "Vault in the copy region."
  value       = aws_backup_vault.copy.arn
}

output "plan_id" {
  description = "Backup plan id."
  value       = aws_backup_plan.this.id
}

output "settings" {
  description = "Effective settings, for plan assertions in consuming stacks."
  value = {
    schedule       = var.schedule
    retention_days = var.retention_days
    resources      = var.resource_arns
    copy_vault     = aws_backup_vault.copy.name
  }
}

output "role_permissions_boundaries" {
  description = "Permissions boundary of each IAM role the module created, for plan assertions in consuming stacks."
  value       = [aws_iam_role.this.permissions_boundary]
}
