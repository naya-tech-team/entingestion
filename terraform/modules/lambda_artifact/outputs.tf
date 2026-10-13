output "code" {
  description = "The package for modules/lambda_function: zip_path or s3_bucket + s3_key, with source_code_hash."
  value = {
    zip_path         = local.use_s3 ? null : var.zip_path
    s3_bucket        = local.use_s3 ? var.s3_bucket : null
    s3_key           = var.s3_key
    source_code_hash = local.use_s3 ? var.s3_source_code_hash : local.zip_hash
  }
}

output "mode" {
  description = "s3 or zip."
  value       = local.use_s3 ? "s3" : "zip"
}
