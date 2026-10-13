terraform {
  required_version = ">= 1.10"
}

variable "s3_bucket" {
  description = "Artifact bucket."
  type        = string
  default     = "example-artifacts"
}

variable "s3_key" {
  description = "Artifact key."
  type        = string
  default     = "example/abc123.zip"
}

variable "s3_source_code_hash" {
  description = "base64 SHA-256 of the artifact."
  type        = string
  default     = "47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="
}

module "package" {
  source = "../.."

  s3_bucket           = var.s3_bucket
  s3_key              = var.s3_key
  s3_source_code_hash = var.s3_source_code_hash
}

output "code" {
  description = "Pass this as modules/lambda_function's code."
  value       = module.package.code
}
