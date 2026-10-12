variable "function_name" {
  description = "Function (name or ARN) that consumes the queue."
  type        = string
}

variable "queue_arn" {
  description = "SQS queue to consume."
  type        = string
}

variable "batch_size" {
  description = "Messages per invocation."
  type        = number
  default     = 10

  validation {
    condition     = var.batch_size >= 1 && var.batch_size <= 10000
    error_message = "batch_size must be between 1 and 10000."
  }
}

variable "batching_window_seconds" {
  description = "How long to gather a batch before invoking (0-300). Required > 0 when batch_size > 10."
  type        = number
  default     = 0

  validation {
    condition     = var.batching_window_seconds >= 0 && var.batching_window_seconds <= 300
    error_message = "batching_window_seconds must be between 0 and 300."
  }
}

variable "max_concurrency" {
  description = "Upper bound on concurrent invocations from this queue (2-1000); null = no bound."
  type        = number
  default     = null

  validation {
    condition     = var.max_concurrency == null || (coalesce(var.max_concurrency, 2) >= 2 && coalesce(var.max_concurrency, 2) <= 1000)
    error_message = "max_concurrency must be between 2 and 1000."
  }
}

variable "enabled" {
  description = "Pause consumption without deleting the mapping."
  type        = bool
  default     = true
}
