variable "name" {
  description = "Globally unique bucket name."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.name))
    error_message = "name must be a valid S3 bucket name (3-63 lowercase letters, digits, dots, hyphens)."
  }
}

variable "kms_key_arn" {
  description = "KMS key used for default SSE-KMS encryption (with an S3 bucket key). null = SSE-S3 (AES256): only for content that isn't sensitive, such as a static website."
  type        = string
  default     = null
}

variable "versioning" {
  description = "Enable object versioning."
  type        = bool
  default     = true
}

variable "force_destroy" {
  description = "Allow destroying a non-empty bucket. Keep false for buckets that hold real data."
  type        = bool
  default     = false
}

variable "lifecycle_rules" {
  description = "Lifecycle rules. An empty prefix applies the rule to the whole bucket."
  type = list(object({
    id                                     = string
    prefix                                 = optional(string, "")
    tags                                   = optional(map(string), {}) # objects with all of these tags
    abort_incomplete_multipart_upload_days = optional(number)
    expiration_days                        = optional(number)
    noncurrent_version_expiration_days     = optional(number)
    noncurrent_version_transitions = optional(list(object({
      days          = number
      storage_class = string
    })), [])
  }))
  default = []

  # S3 refuses abort_incomplete_multipart_upload on tag-filtered rules, so a bucket with any
  # tag-filtered rule must abort multipart uploads in an untagged rule.
  validation {
    condition = (
      length([for r in var.lifecycle_rules : r if length(r.tags) > 0]) == 0
      || length([for r in var.lifecycle_rules : r if length(r.tags) == 0 && r.abort_incomplete_multipart_upload_days != null]) > 0
    )
    error_message = "With tag-filtered lifecycle rules, add an untagged rule with abort_incomplete_multipart_upload_days."
  }

  validation {
    condition     = alltrue([for r in var.lifecycle_rules : length(r.tags) == 0 || r.abort_incomplete_multipart_upload_days == null])
    error_message = "abort_incomplete_multipart_upload_days can't be set on a tag-filtered rule (S3 refuses it)."
  }
}

variable "cors_rules" {
  description = "CORS rules, for example to allow browser uploads through presigned URLs."
  type = list(object({
    allowed_methods = list(string)
    allowed_origins = list(string)
    allowed_headers = optional(list(string), [])
    expose_headers  = optional(list(string), [])
    max_age_seconds = optional(number, 3000)
  }))
  default = []
}

variable "policy_documents" {
  description = "Extra IAM policy documents (JSON) merged into the bucket policy, after the TLS-only statement."
  type        = list(string)
  default     = []
}

variable "access_logging" {
  description = "Optional server access logging target."
  type = object({
    target_bucket = string
    target_prefix = string
  })
  default = null
}
