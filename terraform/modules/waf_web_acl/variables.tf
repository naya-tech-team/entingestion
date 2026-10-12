variable "name" {
  description = "Web ACL name."
  type        = string
}

variable "resource_arns" {
  description = "Regional resources to protect (API Gateway stage ARNs, ALBs, ...). Keys are stable labels."
  type        = map(string)
  default     = {}
}

variable "managed_rule_groups" {
  description = <<-EOT
    AWS managed rule groups, evaluated in priority order (lowest first). count_rules switches
    individual rules to COUNT, for rules that would block legitimate traffic.
  EOT
  type = list(object({
    name        = string
    priority    = number
    vendor_name = optional(string, "AWS")
    count_rules = optional(list(string), [])
  }))
  default = [
    { name = "AWSManagedRulesAmazonIpReputationList", priority = 10 },
    { name = "AWSManagedRulesKnownBadInputsRuleSet", priority = 20 },
    { name = "AWSManagedRulesCommonRuleSet", priority = 30 },
  ]
}

variable "rate_limit" {
  description = "Requests per source IP per 5 minutes before blocking; null disables the rate rule."
  type        = number
  default     = 2000

  validation {
    condition     = var.rate_limit == null || coalesce(var.rate_limit, 100) >= 100
    error_message = "rate_limit must be at least 100 (the WAF minimum)."
  }
}

variable "log_retention_days" {
  description = "Retention for the WAF log group."
  type        = number
  default     = 30
}

variable "log_kms_key_arn" {
  description = "KMS key for the WAF log group."
  type        = string
}

variable "redacted_headers" {
  description = "Request headers redacted from WAF logs (credentials, signatures)."
  type        = list(string)
  default     = ["authorization", "x-amz-security-token"]
}
