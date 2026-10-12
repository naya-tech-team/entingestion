variable "name" {
  description = "Cluster identifier (also prefixes instances, subnet group, parameter group)."
  type        = string
}

variable "engine_version" {
  description = "Aurora PostgreSQL version."
  type        = string
  default     = "16.8"
}

variable "database_name" {
  description = "Initial database."
  type        = string
}

variable "master_username" {
  description = "Master user; its password is generated and kept in Secrets Manager (manage_master_user_password)."
  type        = string
  default     = "postgres_admin"
}

variable "min_acu" {
  description = "Serverless v2 minimum capacity (ACU). 0 enables auto-pause, which RDS Proxy prevents."
  type        = number
  default     = 0.5
}

variable "max_acu" {
  description = "Serverless v2 maximum capacity (ACU)."
  type        = number
  default     = 4
}

variable "instance_count" {
  description = "Instances (1 writer + readers)."
  type        = number
  default     = 1
}

variable "vpc_id" {
  description = "VPC id."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnets in at least two AZs."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "Aurora needs subnets in at least two AZs."
  }
}

variable "allowed_security_group_ids" {
  description = "Client security groups allowed on 5432, keyed by a stable label."
  type        = map(string)
  default     = {}
}

variable "kms_key_arn" {
  description = "KMS key for storage, the master secret, Performance Insights and the log group."
  type        = string
}

variable "deletion_protection" {
  description = "Block cluster deletion."
  type        = bool
  default     = true
}

variable "skip_final_snapshot" {
  description = "Skip the final snapshot on delete (disposable environments only)."
  type        = bool
  default     = false
}

variable "backup_retention_days" {
  description = "Automated backup retention."
  type        = number
  default     = 7
}

variable "log_retention_days" {
  description = "Retention for the exported PostgreSQL log group."
  type        = number
  default     = 30
}

variable "monitoring_interval" {
  description = "Enhanced Monitoring interval in seconds (0 disables it)."
  type        = number
  default     = 60

  validation {
    condition     = contains([0, 1, 5, 10, 15, 30, 60], var.monitoring_interval)
    error_message = "monitoring_interval must be one of 0, 1, 5, 10, 15, 30, 60."
  }
}

variable "parameters" {
  description = "Cluster parameter overrides. Defaults: rds.force_ssl=1, log_statement=ddl, log_min_duration_statement=1000."
  type = map(object({
    value        = string
    apply_method = optional(string, "immediate")
  }))
  default = {}
}

variable "permissions_boundary" {
  description = "IAM policy ARN set as the permissions boundary on every role this module creates. null = no boundary."
  type        = string
  default     = null
}
