# -- Account and tagging ------------------------------------------------------

variable "region" {
  description = "AWS region (the BFF function and the site bucket; CloudFront itself is global)."
  type        = string
}

variable "aws_account_id" {
  description = "Account this environment deploys to. The provider refuses to run against any other."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be a 12-digit account id."
  }
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

variable "tags" {
  description = "Extra tags applied to every resource."
  type        = map(string)
  default     = {}
}

# -- Service ------------------------------------------------------------------

variable "env" {
  description = "Environment name, for example dev or prod."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.env))
    error_message = "env must be 2-16 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "ssm_prefix" {
  description = "SSM Parameter Store prefix. The stack reads <prefix>/<env>/producer/* and <prefix>/<env>/ingestion/*, and publishes <prefix>/<env>/ui/origin."
  type        = string
  default     = "/entingestion"

  validation {
    condition     = startswith(var.ssm_prefix, "/") && !endswith(var.ssm_prefix, "/")
    error_message = "ssm_prefix must start with / and must not end with /."
  }
}

variable "price_class" {
  description = "CloudFront price class. PriceClass_100 (North America and Europe edges) is the cheapest."
  type        = string
  default     = "PriceClass_100"

  validation {
    condition     = contains(["PriceClass_100", "PriceClass_200", "PriceClass_All"], var.price_class)
    error_message = "price_class must be PriceClass_100, PriceClass_200 or PriceClass_All."
  }
}

# -- BFF Lambda package: a local zip (default) or an artifact in S3 (CI) ------

variable "lambda_zip_path" {
  description = "Path to dist/bff_lambda.zip (built by services/ui/bff/scripts/build_lambda.sh), relative to this directory."
  type        = string
  default     = null
}

variable "lambda_s3_bucket" {
  description = "Artifact bucket holding the zip (alternative to lambda_zip_path)."
  type        = string
  default     = null
}

variable "lambda_s3_key" {
  description = "Artifact key, for example ui-bff/<git_sha>.zip."
  type        = string
  default     = null
}

variable "lambda_s3_source_code_hash" {
  description = "base64 SHA-256 of the artifact (openssl dgst -sha256 -binary zip | base64)."
  type        = string
  default     = null
}

variable "log_retention_days" {
  description = "Retention for the BFF log group."
  type        = number
  default     = 30
}

variable "log_level" {
  description = "POWERTOOLS_LOG_LEVEL for the BFF."
  type        = string
  default     = "INFO"
}

variable "bff_memory_size" {
  description = "BFF memory in MB. It only forwards requests, so little is needed."
  type        = number
  default     = 256
}

# -- Hardening ----------------------------------------------------------------

variable "workload_boundary_enabled" {
  description = "Set a permissions boundary on every IAM role the stack creates. The deploy role may only create roles with entingestion-<env>-workload-boundary (terraform/bootstrap), so turn this off only where that bootstrap isn't used."
  type        = bool
  default     = true
}

variable "permissions_boundary_arn" {
  description = "Boundary policy ARN for the stack's roles. null = arn:<partition>:iam::<account>:policy/entingestion-<env>-workload-boundary."
  type        = string
  default     = null
}
