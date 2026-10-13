variable "zip_path" {
  description = "A local zip (the build script's output), relative to the calling stack's directory. Used when s3_key is null."
  type        = string
  default     = null
}

variable "s3_bucket" {
  description = "Artifact bucket holding the zip (instead of zip_path)."
  type        = string
  default     = null
}

variable "s3_key" {
  description = "Artifact key, for example producer/<git_sha>.zip. Set: S3 mode; null: zip mode."
  type        = string
  default     = null

  validation {
    condition     = var.s3_key != null || var.zip_path != null
    error_message = "Set zip_path (a local build), or s3_bucket, s3_key and s3_source_code_hash (an artifact in S3)."
  }

  validation {
    condition     = var.s3_key == null || (var.s3_bucket != null && var.s3_source_code_hash != null)
    error_message = "An S3 artifact needs s3_bucket and s3_source_code_hash as well as s3_key."
  }
}

variable "s3_source_code_hash" {
  description = "base64 SHA-256 of the S3 artifact (openssl dgst -sha256 -binary zip | base64)."
  type        = string
  default     = null
}
