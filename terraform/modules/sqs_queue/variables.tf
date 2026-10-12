variable "name" {
  description = "Queue name; the dead-letter queue is <name>-dlq."
  type        = string
}

variable "visibility_timeout_seconds" {
  description = "Visibility timeout. For a Lambda consumer, use at least 6x the function timeout."
  type        = number
  default     = 30
}

variable "message_retention_seconds" {
  description = "Retention on the main queue."
  type        = number
  default     = 345600 # 4 days
}

variable "max_receive_count" {
  description = "Receives before a message moves to the dead-letter queue."
  type        = number
  default     = 5

  validation {
    condition     = var.max_receive_count >= 1 && var.max_receive_count <= 1000
    error_message = "max_receive_count must be between 1 and 1000."
  }
}

variable "dlq_message_retention_seconds" {
  description = "Retention on the dead-letter queue."
  type        = number
  default     = 1209600 # 14 days, the SQS maximum
}

variable "policy_documents" {
  description = "IAM policy documents (JSON) merged into the main queue's resource policy (next to the TLS-only statement), for example to let S3 send to it."
  type        = list(string)
  default     = []
}

variable "kms_key_arn" {
  description = "Customer managed KMS key for both queues. null = SQS-managed encryption (SSE-SQS). With a key, every sender and consumer needs access to it, and AWS services that send (S3, SNS, EventBridge) need key policy grants."
  type        = string
  default     = null
}

variable "kms_data_key_reuse_period_seconds" {
  description = "How long SQS reuses a data key before calling KMS again (kms_key_arn only)."
  type        = number
  default     = 300

  validation {
    condition     = var.kms_data_key_reuse_period_seconds >= 60 && var.kms_data_key_reuse_period_seconds <= 86400
    error_message = "kms_data_key_reuse_period_seconds must be between 60 and 86400."
  }
}
