output "id" {
  description = "Bucket name."
  value       = aws_s3_bucket.this.id
}

output "arn" {
  description = "Bucket ARN."
  value       = aws_s3_bucket.this.arn
}

output "regional_domain_name" {
  description = "Regional domain name of the bucket."
  value       = aws_s3_bucket.this.bucket_regional_domain_name
}

output "settings" {
  description = "Effective bucket settings, for plan assertions in consuming stacks."
  value = {
    force_destroy     = aws_s3_bucket.this.force_destroy
    sse_algorithm     = var.kms_key_arn != null ? "aws:kms" : "AES256"
    versioning_status = aws_s3_bucket_versioning.this.versioning_configuration[0].status
    lifecycle_rules   = var.lifecycle_rules
    cors_rules        = var.cors_rules
  }
}
