output "raw_bucket_name" {
  description = "Raw upload bucket name."
  value       = module.raw_bucket.id
}

output "raw_bucket_arn" {
  description = "Raw upload bucket ARN."
  value       = module.raw_bucket.arn
}

output "process_table_name" {
  description = "process table name."
  value       = module.process_table.name
}

output "process_table_arn" {
  description = "process table ARN."
  value       = module.process_table.arn
}

output "articles_table_name" {
  description = "articles table name."
  value       = module.articles_table.name
}

output "articles_table_arn" {
  description = "articles table ARN."
  value       = module.articles_table.arn
}

output "articles_stream_arn" {
  description = "articles stream ARN (NEW_AND_OLD_IMAGES), the ingestion pipeline's input."
  value       = module.articles_table.stream_arn
}

output "uploads_table_name" {
  description = "producer_uploads table name."
  value       = module.uploads_table.name
}

output "uploads_table_arn" {
  description = "producer_uploads table ARN."
  value       = module.uploads_table.arn
}

output "kms_key_arn" {
  description = "Producer data KMS key ARN."
  value       = module.kms.key_arn
}

output "ssm_parameter_names" {
  description = "SSM parameters published for other stacks."
  value       = { for k, p in aws_ssm_parameter.published : k => p.name }
}

output "api_url" {
  description = "API stage invoke URL."
  value       = module.api.invoke_url
}

output "invoke_policy_arn" {
  description = "Writer policy (every route): attach to principals that create processes and upload. Which sources they may use is set by source_access."
  value       = module.api.invoke_policy_arn
}

output "invoke_reader_policy_arn" {
  description = "Reader policy (GET routes only)."
  value       = aws_iam_policy.invoke_reader.arn
}

output "api_function_name" {
  description = "API Lambda function name."
  value       = module.api_function.function_name
}

output "registrar_queue_url" {
  description = "Registrar queue (S3 ObjectCreated notifications)."
  value       = module.registrar_queue.queue_url
}

output "registrar_dlq_url" {
  description = "Registrar dead-letter queue: messages that failed 5 times."
  value       = module.registrar_queue.dlq_url
}

output "registrar_function_name" {
  description = "Registrar Lambda function name."
  value       = module.registrar_function.function_name
}

output "alarm_topic_arn" {
  description = "SNS topic every alarm notifies."
  value       = module.alarms.topic_arn
}

output "web_acl_arn" {
  description = "WAF web ACL protecting the API."
  value       = coalesce(var.waf_acl_arn, one(module.waf[*].web_acl_arn))
}

output "janitor_function_name" {
  description = "Producer janitor Lambda (run on demand to settle tickets now)."
  value       = module.janitor_function.function_name
}

