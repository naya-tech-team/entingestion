variable "name" {
  description = "Schedule name (also prefixes the scheduler role)."
  type        = string
}

variable "description" {
  description = "Schedule description."
  type        = string
  default     = ""
}

variable "schedule_expression" {
  description = "rate(...), cron(...) or at(...), for example \"rate(1 hour)\"."
  type        = string

  validation {
    condition     = can(regex("^(rate|cron|at)\\(.+\\)$", var.schedule_expression))
    error_message = "schedule_expression must be rate(...), cron(...) or at(...)."
  }
}

variable "timezone" {
  description = "Time zone for cron and at expressions."
  type        = string
  default     = "UTC"
}

variable "target_arn" {
  description = "What the schedule calls, for example a Lambda function ARN."
  type        = string
}

variable "target_actions" {
  description = "Actions the scheduler role may perform on target_arn."
  type        = list(string)
  default     = ["lambda:InvokeFunction"]
}

variable "input" {
  description = "JSON sent to the target on every run; null sends the default schedule payload."
  type        = string
  default     = null
}

variable "kms_key_arn" {
  description = "Customer managed key that encrypts the schedule's input."
  type        = string
}

variable "enabled" {
  description = "ENABLED or DISABLED, without deleting the schedule."
  type        = bool
  default     = true
}

variable "flexible_window_minutes" {
  description = "Let the scheduler start up to N minutes late to spread load; null = exact time."
  type        = number
  default     = null

  validation {
    condition     = var.flexible_window_minutes == null || (coalesce(var.flexible_window_minutes, 1) >= 1 && coalesce(var.flexible_window_minutes, 1) <= 1440)
    error_message = "flexible_window_minutes must be between 1 and 1440."
  }
}

variable "retry_attempts" {
  description = "Retries when the target call fails (0-185)."
  type        = number
  default     = 2
}

variable "max_event_age_seconds" {
  description = "Give up on a run that couldn't be delivered for this long (60-86400)."
  type        = number
  default     = 3600
}

variable "dead_letter_queue_arn" {
  description = "SQS queue for runs that couldn't be delivered; null = none."
  type        = string
  default     = null
}

variable "permissions_boundary" {
  description = "IAM policy ARN set as the permissions boundary on every role this module creates. null = no boundary."
  type        = string
  default     = null
}
