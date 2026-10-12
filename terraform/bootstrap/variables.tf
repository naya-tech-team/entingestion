variable "region" {
  description = "Region for the state bucket."
  type        = string
}

variable "aws_account_id" {
  description = "Account this bootstrap is applied in: TF_VAR_aws_account_id (export it from AWS_ACCOUNT_ID; never a tracked file). Guards against applying with the wrong credentials, and every ARN is derived from it."
  type        = string
}

variable "mandatory_tags" {
  description = "The organization's required tags, applied to every resource through the provider's default_tags. Name comes from the resource itself, or else the stack."
  type = object({
    created_by           = string
    created_date         = string # YYYY-MM-DD: when this stack was first applied in this environment
    owner                = string
    internal_client_code = string
    client               = string
    project_name         = string
    division             = string
  })

  validation {
    condition     = alltrue([for v in values(var.mandatory_tags) : trimspace(v) != ""])
    error_message = "Every mandatory tag needs a value: created_by, created_date, owner, internal_client_code, client, project_name, division."
  }

  validation {
    condition     = can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}$", var.mandatory_tags.created_date))
    error_message = "mandatory_tags.created_date must be YYYY-MM-DD."
  }

  validation {
    condition     = alltrue([for v in values(var.mandatory_tags) : can(regex("^[\\p{L}\\p{Z}\\p{N}_.:/=+@-]{0,256}$", v))])
    error_message = "Tag values may contain only letters, digits, spaces and _ . : / = + - @ (up to 256 characters)."
  }
}

variable "deploy_principal_arns" {
  description = "IAM users or roles (people) who may assume the deploy roles and use the state bucket directly (break-glass): \"user/<name>\" or \"role/<name>\" in this account (the ARN is derived from aws_account_id), or a full ARN. From the environment, never a tracked file: TF_VAR_deploy_principal_arns='[\"user/<you>\"]'. The admin applying must be one of them."
  type        = list(string)

  validation {
    condition     = length(var.deploy_principal_arns) > 0 && alltrue([for a in var.deploy_principal_arns : can(regex("^(arn:aws[a-z-]*:iam::[0-9]{12}:)?(user|role)/", a))])
    error_message = "deploy_principal_arns must list at least one IAM user or role: \"user/<name>\", \"role/<name>\" or a full ARN."
  }
}

variable "environments" {
  description = "One deploy role per environment: entingestion-<env>-deploy."
  type        = list(string)
  default     = ["dev", "prod"]

  validation {
    condition     = length(var.environments) > 0 && alltrue([for e in var.environments : can(regex("^[a-z][a-z0-9]{1,15}$", e))])
    error_message = "environments must be short lowercase names such as dev or prod."
  }
}

variable "require_mfa" {
  description = "Deploy roles may only be assumed by sessions that signed in with MFA (aws:MultiFactorAuthPresent)."
  type        = bool
  default     = true
}

variable "github_repository" {
  description = "GitHub owner/repo whose Actions may assume the deploy roles via OIDC (from a GitHub environment named like the env). null = no GitHub trust and no OIDC provider."
  type        = string
  default     = null

  validation {
    condition     = var.github_repository == null || can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", coalesce(var.github_repository, "x/y")))
    error_message = "github_repository must look like owner/repo."
  }
}

variable "table_prefixes" {
  description = "DynamoDB table prefix per environment, as in envs/<env>/*.tfvars (table_prefix). The boundaries deny each environment the other environments' tables by name. An environment not listed is assumed to use \"<env>-\"."
  type        = map(string)
  default     = { dev = "dev-", prod = "" }
}

# -- Audit trail (trail.tf) -------------------------------------------------------

variable "trail_retention_days" {
  description = "Days the account trail's log files are kept in S3."
  type        = number
  default     = 365

  validation {
    condition     = var.trail_retention_days >= 90 && var.trail_retention_days <= 3650
    error_message = "trail_retention_days must be between 90 and 3650."
  }
}

variable "trail_data_event_bucket_arns" {
  description = "Buckets (ARNs) whose object-level reads and writes the trail also records. The state bucket is always included. Each adds data-event charges ($0.10 per 100,000 events)."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for a in var.trail_data_event_bucket_arns : can(regex("^arn:aws[a-z-]*:s3:::[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", a))])
    error_message = "trail_data_event_bucket_arns must be S3 bucket ARNs (arn:aws:s3:::name), without a key."
  }
}
