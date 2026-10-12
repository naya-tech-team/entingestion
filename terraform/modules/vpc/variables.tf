variable "name" {
  description = "Name prefix for the VPC and its subnets."
  type        = string
}

variable "cidr_block" {
  description = "VPC CIDR."
  type        = string
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrhost(var.cidr_block, 0))
    error_message = "cidr_block must be a valid IPv4 CIDR."
  }
}

variable "az_count" {
  description = "Number of Availability Zones (one private subnet each)."
  type        = number
  default     = 3

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 6
    error_message = "az_count must be between 2 and 6 (Aurora needs at least 2)."
  }
}

variable "subnet_newbits" {
  description = "Bits added to the VPC prefix for each subnet (8 on a /16 gives /24 subnets)."
  type        = number
  default     = 8
}

variable "flow_log_retention_days" {
  description = "Retention for VPC flow logs."
  type        = number
  default     = 30
}

variable "kms_key_arn" {
  description = "KMS key for the flow log group."
  type        = string
}

variable "permissions_boundary" {
  description = "IAM policy ARN set as the permissions boundary on every role this module creates. null = no boundary."
  type        = string
  default     = null
}
