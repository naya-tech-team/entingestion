variable "name" {
  description = "Function name."
  type        = string
}

variable "description" {
  description = "Function description."
  type        = string
  default     = ""
}

variable "handler" {
  description = "Handler, for example package.module.lambda_handler."
  type        = string
}

variable "runtime" {
  description = "Lambda runtime."
  type        = string
  default     = "python3.12"
}

variable "architecture" {
  description = "arm64 or x86_64."
  type        = string
  default     = "arm64"

  validation {
    condition     = contains(["arm64", "x86_64"], var.architecture)
    error_message = "architecture must be arm64 or x86_64."
  }
}

variable "memory_size" {
  description = "Memory in MB."
  type        = number
  default     = 512
}

variable "timeout" {
  description = "Timeout in seconds."
  type        = number
  default     = 30

  validation {
    condition     = var.timeout >= 1 && var.timeout <= 900
    error_message = "timeout must be between 1 and 900 seconds."
  }
}

variable "environment" {
  description = "Environment variables."
  type        = map(string)
  default     = {}

  validation {
    condition     = sum(concat([0], [for k, v in var.environment : length(k) + length(v)])) <= 4096
    error_message = "Environment variables are limited to 4 KB in all (names and values)."
  }
}

variable "code" {
  description = <<-EOT
    Deployment package, one of:
      - zip_path: a local zip
      - s3_bucket + s3_key: a zip in S3
      - image_uri: a container image (ECR); handler becomes the image command
    source_code_hash (base64 SHA-256 of the zip, i.e. filebase64sha256) is required for zips, so
    the function updates only when the code changes. Images are pinned by their digest instead.
  EOT
  type = object({
    zip_path         = optional(string)
    s3_bucket        = optional(string)
    s3_key           = optional(string)
    image_uri        = optional(string)
    source_code_hash = optional(string)
  })

  validation {
    condition = (
      length(compact([var.code.zip_path, var.code.s3_bucket, var.code.image_uri])) == 1
      && (var.code.s3_bucket == null) == (var.code.s3_key == null)
    )
    error_message = "Set exactly one of zip_path, s3_bucket + s3_key, or image_uri."
  }

  validation {
    condition     = var.code.image_uri != null || var.code.source_code_hash != null
    error_message = "source_code_hash is required for zip packages."
  }
}

variable "policy_json" {
  description = "IAM policy (JSON) with the function's own permissions. Logs and X-Ray permissions are added by the module."
  type        = string
}

variable "kms_key_arn" {
  description = "KMS key for environment variables and the log group."
  type        = string
}

variable "log_retention_days" {
  description = "Log group retention."
  type        = number
  default     = 30
}

variable "vpc_config" {
  description = "Run in a VPC (private subnets + security groups). Adds the ENI permissions Lambda needs."
  type = object({
    subnet_ids         = list(string)
    security_group_ids = list(string)
  })
  default = null

  validation {
    condition     = var.vpc_config == null || (length(try(var.vpc_config.subnet_ids, [])) > 0 && length(try(var.vpc_config.security_group_ids, [])) > 0)
    error_message = "vpc_config needs at least one subnet and one security group."
  }
}

variable "tracing_mode" {
  description = "X-Ray tracing: Active, or PassThrough for functions that can't reach X-Ray (e.g. in a VPC without an X-Ray endpoint)."
  type        = string
  default     = "Active"

  validation {
    condition     = contains(["Active", "PassThrough"], var.tracing_mode)
    error_message = "tracing_mode must be Active or PassThrough."
  }
}

variable "layers" {
  description = "Lambda layer ARNs (zip packages only)."
  type        = list(string)
  default     = []
}

variable "reserved_concurrent_executions" {
  description = "Reserved concurrency; -1 leaves the function unreserved."
  type        = number
  default     = -1
}

variable "alias_name" {
  description = "Alias that callers invoke; each apply points it at the newly published version."
  type        = string
  default     = "live"

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_-]{0,127}$", var.alias_name))
    error_message = "alias_name must start with a letter (then letters, digits, - and _)."
  }
}

variable "permissions_boundary" {
  description = "IAM policy ARN set as the permissions boundary on every role this module creates. null = no boundary."
  type        = string
  default     = null
}
