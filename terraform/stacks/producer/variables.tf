# -- Account and tagging ------------------------------------------------------

variable "region" {
  description = "AWS region."
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
  description = "Environment name, for example dev or prod. prod turns on deletion protection and disables force_destroy."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.env))
    error_message = "env must be 2-16 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "table_prefix" {
  description = "Prefix for the DynamoDB table names (process, articles, producer_uploads). Use it to separate environments that share an account."
  type        = string
  default     = ""
}

variable "raw_bucket_name" {
  description = "Raw upload bucket name. Defaults to producer-<env>-raw-<account_id>-<region>."
  type        = string
  default     = null
}

variable "ui_extra_origins" {
  description = "Browser origins allowed to PUT files to the raw bucket with presigned URLs, besides the upload UI's own (read from SSM <ssm_prefix>/<env>/ui/origin), for example a local dev server. CORS is off when there are none."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for o in var.ui_extra_origins : startswith(o, "https://") || startswith(o, "http://localhost")])
    error_message = "ui_extra_origins must be https:// origins, or http://localhost for local development."
  }
}

variable "ssm_prefix" {
  description = "SSM Parameter Store prefix for values other stacks consume (for example the ingestion pipeline)."
  type        = string
  default     = "/entingestion"

  validation {
    condition     = startswith(var.ssm_prefix, "/") && !endswith(var.ssm_prefix, "/")
    error_message = "ssm_prefix must start with / and must not end with /."
  }
}

# -- Lambda package: a local zip (default) or an artifact in S3 (CI) ---------

variable "lambda_zip_path" {
  description = "Path to dist/producer_lambda.zip (built by services/producer/scripts/build_lambda.sh), relative to this directory."
  type        = string
  default     = null
}

variable "lambda_s3_bucket" {
  description = "Artifact bucket holding the zip (alternative to lambda_zip_path)."
  type        = string
  default     = null
}

variable "lambda_s3_key" {
  description = "Artifact key, for example producer/<git_sha>.zip."
  type        = string
  default     = null
}

variable "lambda_s3_source_code_hash" {
  description = "base64 SHA-256 of the artifact (openssl dgst -sha256 -binary zip | base64)."
  type        = string
  default     = null
}

variable "log_retention_days" {
  description = "Retention for Lambda and API Gateway log groups."
  type        = number
  default     = 30
}

variable "log_level" {
  description = "POWERTOOLS_LOG_LEVEL for the functions."
  type        = string
  default     = "INFO"
}

variable "max_file_bytes" {
  description = "Largest accepted upload. 5 GiB is the S3 single-PUT limit."
  type        = number
  default     = 5368709120

  validation {
    condition     = var.max_file_bytes > 0 && var.max_file_bytes <= 5368709120
    error_message = "max_file_bytes must be between 1 and 5368709120 (single PUT limit)."
  }
}

variable "allowed_content_types" {
  description = "Content types accepted for upload. Empty uses the application default (brief §4)."
  type        = list(string)
  default     = []
}

variable "upload_url_ttl_seconds" {
  description = "Lifetime of presigned upload URLs."
  type        = number
  default     = 900
}

variable "api_throttle_rate" {
  description = "API steady-state requests per second."
  type        = number
  default     = 50
}

variable "api_throttle_burst" {
  description = "API burst limit."
  type        = number
  default     = 100
}

variable "manage_apigw_account" {
  description = "Create the account-level API Gateway CloudWatch role. Leave false if the account already has one."
  type        = bool
  default     = false
}

# -- Hardening ----------------------------------------------------------------

variable "waf_acl_arn" {
  description = "Existing regional WAF web ACL to attach to the API. When null, the stack creates one."
  type        = string
  default     = null
}

variable "waf_rate_limit" {
  description = "Requests per source IP per 5 minutes before WAF blocks (created ACL only)."
  type        = number
  default     = 2000
}

variable "alarm_emails" {
  description = "Email addresses subscribed to the alarm topic (each must confirm). Required in prod."
  type        = list(string)
  default     = []

  # Alarms nobody receives are worse than none: prod refuses to plan without a subscriber.
  validation {
    condition     = var.env != "prod" || length(var.alarm_emails) > 0
    error_message = "prod needs someone to receive its alarms: set alarm_emails = [\"oncall@example.com\"] in envs/prod/producer.tfvars (each address must confirm the SNS email). See docs/RUNBOOK.md \"Alarms\"."
  }
}

variable "backup_enabled" {
  description = "AWS Backup of the process, articles and uploads tables: daily, kept backup_retention_days, copied to backup_copy_region. prod only (not on the AWS free plan)."
  type        = bool
  default     = false
}

variable "backup_copy_region" {
  description = "Region the backups are copied to (disaster recovery); must differ from region."
  type        = string
  default     = "us-east-2"

  validation {
    condition     = var.backup_copy_region != var.region
    error_message = "backup_copy_region must be another region than region."
  }
}

variable "backup_retention_days" {
  description = "How long backups are kept, in both regions."
  type        = number
  default     = 35
}

variable "alarm_notify_on_ok" {
  description = "Also email when an alarm recovers (OK). Off: one message per incident."
  type        = bool
  default     = false
}

variable "registrar_queue_age_alarm_seconds" {
  description = "Alarm when the oldest S3 event waiting for the registrar is older than this."
  type        = number
  default     = 900
}

variable "registrar_reserved_concurrency" {
  description = "Reserved concurrency for the registrar (prod: so the shared account's other work can't starve uploads). null = unreserved."
  type        = number
  default     = null
}

variable "api_reserved_concurrency" {
  description = "Reserved concurrency for the API function. null = unreserved."
  type        = number
  default     = null
}

# -- Access (review C3) ---------------------------------------------------------

variable "source_access" {
  description = <<-EOT
    Which principals may write and read each source, and the tenant its articles belong to
    (producer.authz). Patterns are shell-style and match the caller ARN or, for an assumed
    role, the role ARN. A source missing here can't be used; an empty map allows nothing.
    Readers and writers of a source may also search its tenant (ingestion POST /v1/search).
    pii: what ingestion does with PII it finds when its pii_screening is on: block (don't
    index), redact (replace with [TYPE]) or allow (the default; record it).
  EOT
  type = map(object({
    tenant_id = string
    writers   = list(string)
    readers   = optional(list(string), [])
    pii       = optional(string, "allow")
  }))
  default = {}

  validation {
    condition     = alltrue([for cfg in values(var.source_access) : contains(["block", "redact", "allow"], cfg.pii)])
    error_message = "Each source's pii must be block, redact or allow."
  }

  validation {
    condition = alltrue([
      for name, cfg in var.source_access :
      can(regex("^[a-z0-9][a-z0-9._-]{0,63}$", name)) && trimspace(cfg.tenant_id) != ""
    ])
    error_message = "Each source name must match ^[a-z0-9][a-z0-9._-]{0,63}$ and have a tenant_id."
  }

  validation {
    condition     = var.env != "prod" || length(var.source_access) > 0
    error_message = "prod needs source_access: without it nobody can use the API."
  }

  validation {
    # Lambda environment variables share a 4 KB budget.
    condition     = length(jsonencode(var.source_access)) <= 2500
    error_message = "source_access is too large for a Lambda environment variable; use patterns."
  }
}

variable "admin_principals" {
  description = "Principals (ARN patterns) that may read and change every source, e.g. the deploy role."
  type        = list(string)
  default     = []
}

# -- Lifecycle (review M2.2) -------------------------------------------------------

variable "janitor_schedule" {
  description = "How often the janitor runs (EventBridge Scheduler expression)."
  type        = string
  default     = "rate(1 hour)"
}

variable "open_process_idle_days" {
  description = "Close OPEN processes nobody has touched for this many days."
  type        = number
  default     = 7

  validation {
    condition     = var.open_process_idle_days >= 1
    error_message = "open_process_idle_days must be at least 1."
  }
}

variable "unused_upload_expiration_days" {
  description = "Delete rejected and superseded raw objects after this many days."
  type        = number
  default     = 30
}

variable "raw_noncurrent_expiration_days" {
  description = "Delete noncurrent raw object versions after this many days (they move to Glacier IR at 30; its minimum is 90)."
  type        = number
  default     = 365

  validation {
    condition     = var.raw_noncurrent_expiration_days >= 120
    error_message = "Keep noncurrent versions at least 120 days (Glacier IR bills 90 days minimum after the 30-day transition)."
  }
}

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
