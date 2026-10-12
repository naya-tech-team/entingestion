output "url" {
  description = "The upload UI (make ui-url)."
  value       = "https://${aws_cloudfront_distribution.this.domain_name}"
}

output "distribution_id" {
  description = "CloudFront distribution id (make ui-deploy invalidates it)."
  value       = aws_cloudfront_distribution.this.id
}

output "site_bucket" {
  description = "Bucket holding the built SPA (make ui-deploy syncs it)."
  value       = module.site_bucket.id
}

output "kvs_arn" {
  description = "KeyValueStore holding the basic-auth hash (make ui-password writes it)."
  value       = aws_cloudfront_key_value_store.auth.arn
}

output "bff_function_name" {
  description = "BFF Lambda function name."
  value       = module.bff.function_name
}

output "bff_role_arn" {
  description = "The BFF's role, the UI's static user: list it as a writer in the producer's source_access."
  value       = module.bff.role_arn
}
