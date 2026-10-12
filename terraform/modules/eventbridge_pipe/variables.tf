variable "name" {
  description = "Pipe name."
  type        = string
}

variable "source_stream_arn" {
  description = "DynamoDB stream ARN (source)."
  type        = string
}

variable "target_queue_arn" {
  description = "SQS queue ARN (target)."
  type        = string
}

variable "filter_patterns" {
  description = "Event filter patterns (JSON strings); a record passes if it matches any. Empty = no filtering."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for p in var.filter_patterns : can(jsondecode(p))])
    error_message = "Every filter pattern must be valid JSON."
  }
}

variable "dlq_arn" {
  description = "SQS dead-letter queue for records that still fail after max_retries."
  type        = string
}

variable "starting_position" {
  description = "Where to start reading the stream: LATEST or TRIM_HORIZON."
  type        = string
  default     = "LATEST"

  validation {
    condition     = contains(["LATEST", "TRIM_HORIZON"], var.starting_position)
    error_message = "starting_position must be LATEST or TRIM_HORIZON."
  }
}

variable "batch_size" {
  description = "Records per batch."
  type        = number
  default     = 10
}

variable "batching_window_seconds" {
  description = "Seconds to gather a batch."
  type        = number
  default     = 0
}

variable "max_retries" {
  description = "Retries for a failing batch before records go to the DLQ."
  type        = number
  default     = 5
}

variable "max_record_age_seconds" {
  description = "Records older than this go to the DLQ (-1 = no limit; streams keep 24 h)."
  type        = number
  default     = 86400
}

variable "input_template" {
  description = "Optional input transformer template; null forwards the stream record as-is."
  type        = string
  default     = null
}

variable "policy_documents" {
  description = "Extra IAM policy documents (JSON) for the pipe role, e.g. kms:Decrypt on the source table's key."
  type        = list(string)
  default     = []
}

variable "log_level" {
  description = "Pipe execution log level: OFF, ERROR, INFO or TRACE."
  type        = string
  default     = "ERROR"

  validation {
    condition     = contains(["OFF", "ERROR", "INFO", "TRACE"], var.log_level)
    error_message = "log_level must be OFF, ERROR, INFO or TRACE."
  }
}

variable "log_retention_days" {
  description = "Retention for the pipe's log group."
  type        = number
  default     = 30
}

variable "kms_key_arn" {
  description = "KMS key for the pipe's log group."
  type        = string
}

variable "permissions_boundary" {
  description = "IAM policy ARN set as the permissions boundary on every role this module creates. null = no boundary."
  type        = string
  default     = null
}
