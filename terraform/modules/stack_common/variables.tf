variable "stack" {
  description = "Stack name, the first part of every resource name: <stack>-<env>-..."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}$", var.stack))
    error_message = "stack must be 2-31 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "env" {
  description = "Environment name, for example dev or prod. prod is the protected one (is_prod)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.env))
    error_message = "env must be 2-16 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "aws_account_id" {
  description = "The account the stack deploys to. Boundary and principal ARNs are built from it."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be a 12-digit account id."
  }
}

variable "partition" {
  description = "AWS partition of the account (aws, aws-cn, aws-us-gov)."
  type        = string
  default     = "aws"
}

variable "mandatory_tags" {
  description = "The organization's required tags. The calling stack validates the values."
  type = object({
    created_by           = string
    created_date         = string
    owner                = string
    internal_client_code = string
    client               = string
    project_name         = string
    division             = string
  })
}

variable "tags" {
  description = "Extra tags for every resource. The mandatory tags and env win over them."
  type        = map(string)
  default     = {}
}

variable "workload_boundary_enabled" {
  description = "Whether the stack's roles carry the workload permissions boundary."
  type        = bool
  default     = true
}

variable "permissions_boundary_arn" {
  description = "Boundary policy ARN for the stack's roles. null = this account's entingestion-<env>-workload-boundary (terraform/bootstrap)."
  type        = string
  default     = null
}

variable "principals" {
  description = "Lists of IAM principals to expand, by any key: \"role/<name>\" or \"user/<name>\" (patterns allowed) become ARNs in this account; a full ARN is kept as is."
  type        = map(list(string))
  default     = {}
}
