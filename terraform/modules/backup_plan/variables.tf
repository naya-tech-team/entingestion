variable "name" {
  description = "Name of the vaults, plan, selection and role (<stack>-<env>-backup; the deploy roles may only create roles named <stack>-<env>-*)."
  type        = string
}

variable "kms_key_arn" {
  description = "KMS key of the vault in this region."
  type        = string
}

variable "resource_arns" {
  description = "What to back up: Aurora cluster or RDS instance ARNs, DynamoDB table ARNs."
  type        = list(string)

  validation {
    condition     = length(var.resource_arns) > 0
    error_message = "resource_arns needs at least one resource."
  }
}

variable "schedule" {
  description = "When the daily backup starts (cron, UTC)."
  type        = string
  default     = "cron(0 5 * * ? *)"
}

variable "retention_days" {
  description = "How long recovery points are kept, in this region and in the copy region."
  type        = number
  default     = 35

  validation {
    condition     = var.retention_days >= 7
    error_message = "retention_days must be at least 7."
  }
}

variable "permissions_boundary" {
  description = "IAM policy ARN set as the permissions boundary on the backup role. null = no boundary."
  type        = string
  default     = null
}
