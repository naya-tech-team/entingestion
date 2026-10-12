variable "name" {
  description = "REST API name."
  type        = string
}

variable "description" {
  description = "REST API description."
  type        = string
  default     = ""
}

variable "lambda_function_name" {
  description = "Function that receives every request (AWS_PROXY)."
  type        = string
}

variable "lambda_invoke_arn" {
  description = "Integration ARN: the function's invoke_arn, or its alias's (with lambda_qualifier)."
  type        = string
}

variable "lambda_qualifier" {
  description = "Alias that lambda_invoke_arn points at, so API Gateway's invoke permission is granted on it. null = the unqualified function."
  type        = string
  default     = null
}

variable "stage_name" {
  description = "Stage name."
  type        = string
  default     = "prod"
}

variable "throttle_rate_limit" {
  description = "Steady-state requests per second for all methods."
  type        = number
  default     = 50
}

variable "throttle_burst_limit" {
  description = "Burst limit for all methods."
  type        = number
  default     = 100
}

variable "logging_level" {
  description = "Execution logging level: OFF, ERROR or INFO."
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["OFF", "ERROR", "INFO"], var.logging_level)
    error_message = "logging_level must be OFF, ERROR or INFO."
  }
}

variable "log_retention_days" {
  description = "Retention for the access and execution log groups."
  type        = number
  default     = 30
}

variable "log_kms_key_arn" {
  description = "KMS key for the access and execution log groups."
  type        = string
}

variable "manage_apigw_account" {
  description = <<-EOT
    Create the account-level API Gateway CloudWatch Logs role (aws_api_gateway_account). This is a
    per-account, per-region singleton: enable it in exactly one stack, and only if the account
    doesn't already have a role configured.
  EOT
  type        = bool
  default     = false
}

variable "invoke_policy_name" {
  description = "Name of the IAM policy that grants callers execute-api:Invoke."
  type        = string
}

variable "invoke_resource_paths" {
  description = "Paths callers may invoke, relative to the stage, as METHOD/path (for example \"*/v1/*\")."
  type        = list(string)
  default     = ["*/*"]
}

variable "permissions_boundary" {
  description = "IAM policy ARN set as the permissions boundary on every role this module creates. null = no boundary."
  type        = string
  default     = null
}
