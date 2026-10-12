variable "alias" {
  description = "Alias name without the \"alias/\" prefix."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z0-9/_-]+$", var.alias)) && !startswith(var.alias, "aws/")
    error_message = "alias must contain only letters, digits, /, _ and -, and must not start with aws/."
  }
}

variable "description" {
  description = "Key description."
  type        = string
}

variable "deletion_window_in_days" {
  description = "Waiting period before a scheduled key deletion completes."
  type        = number
  default     = 30

  validation {
    condition     = var.deletion_window_in_days >= 7 && var.deletion_window_in_days <= 30
    error_message = "deletion_window_in_days must be between 7 and 30."
  }
}

variable "admin_principals" {
  description = <<-EOT
    IAM role or user ARNs that administer the key (policy, rotation, deletion, tags). Empty (the
    default) keeps the account-root statement that delegates everything to IAM. When set, the
    account root keeps only use (encrypt, decrypt, data keys, grants for AWS resources), so the
    principal applying Terraform must be in this list or it can't update the key policy later.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for a in var.admin_principals : can(regex("^arn:aws[a-z-]*:iam::[0-9]{12}:(role|user)/", a))])
    error_message = "admin_principals must be IAM role or user ARNs."
  }
}

variable "policy_statements" {
  description = <<-EOT
    Extra key policy statements, appended after the default statement that lets the account
    manage the key through IAM. Use these for service principals that act without an IAM role
    (for example CloudWatch Logs or CloudWatch alarms publishing to an encrypted SNS topic).
  EOT
  type = list(object({
    sid     = string
    actions = list(string)
    principals = list(object({
      type        = string
      identifiers = list(string)
    }))
    conditions = optional(list(object({
      test     = string
      variable = string
      values   = list(string)
    })), [])
  }))
  default = []
}
