variable "name" {
  description = "State machine name."
  type        = string
}

variable "definition" {
  description = "Amazon States Language definition (JSON string), e.g. rendered with templatefile()."
  type        = string

  validation {
    condition     = can(jsondecode(var.definition).StartAt)
    error_message = "definition must be ASL JSON with a StartAt."
  }
}

variable "type" {
  description = "STANDARD or EXPRESS."
  type        = string
  default     = "STANDARD"

  validation {
    condition     = contains(["STANDARD", "EXPRESS"], var.type)
    error_message = "type must be STANDARD or EXPRESS."
  }
}

variable "policy_documents" {
  description = "IAM policy documents (JSON) for what the state machine invokes (Lambdas, ECS, SQS, ...)."
  type        = list(string)
  default     = []
}

variable "log_level" {
  description = "Execution log level: ALL, ERROR, FATAL or OFF. ALL records every state transition (without input/output unless include_execution_data)."
  type        = string
  default     = "ALL"

  validation {
    condition     = contains(["ALL", "ERROR", "FATAL", "OFF"], var.log_level)
    error_message = "log_level must be ALL, ERROR, FATAL or OFF."
  }
}

variable "include_execution_data" {
  description = "Log state input/output (may contain document content; keep false unless debugging)."
  type        = bool
  default     = false
}

variable "log_retention_days" {
  description = "Retention for the state machine's log group."
  type        = number
  default     = 30
}

variable "kms_key_arn" {
  description = "KMS key encrypting execution data and the log group."
  type        = string
}

variable "permissions_boundary" {
  description = "IAM policy ARN set as the permissions boundary on every role this module creates. null = no boundary."
  type        = string
  default     = null
}
