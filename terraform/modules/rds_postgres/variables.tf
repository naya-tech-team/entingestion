variable "name" {
  description = "DB instance identifier (also prefixes the subnet group, parameter group, security group)."
  type        = string
}

variable "engine_version" {
  description = "RDS for PostgreSQL version."
  type        = string
  default     = "17.11"
}

variable "instance_class" {
  description = "Instance class (db.t4g.micro fits the AWS free tier)."
  type        = string
  default     = "db.t4g.micro"
}

variable "allocated_storage_gb" {
  description = "Initial storage (GB)."
  type        = number
  default     = 20
}

variable "max_allocated_storage_gb" {
  description = "Storage autoscaling ceiling (GB); equal to allocated_storage_gb disables autoscaling."
  type        = number
  default     = 20
}

variable "database_name" {
  description = "Initial database."
  type        = string
}

variable "master_username" {
  description = "Master user; its password is generated and kept in Secrets Manager."
  type        = string
  default     = "postgres_admin"
}

variable "vpc_id" {
  description = "VPC id."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnets in at least two AZs (required by the subnet group)."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "RDS subnet groups need subnets in at least two AZs."
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

variable "multi_az" {
  description = "Standby in a second AZ."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Block instance deletion."
  type        = bool
  default     = true
}

variable "skip_final_snapshot" {
  description = "Skip the final snapshot on delete (disposable environments only)."
  type        = bool
  default     = false
}

variable "backup_retention_days" {
  description = "Automated backup retention (the AWS free plan allows at most 1)."
  type        = number
  default     = 7
}

variable "performance_insights" {
  description = "Enable Performance Insights (7-day retention)."
  type        = bool
  default     = true
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

variable "log_retention_days" {
  description = "Retention for the exported PostgreSQL log group."
  type        = number
  default     = 30
}

variable "parameters" {
  description = "Parameter overrides. Defaults: rds.force_ssl=1, log_statement=ddl, log_min_duration_statement=1000."
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
