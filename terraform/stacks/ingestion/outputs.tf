output "state_machine_arn" {
  description = "Ingest workflow."
  value       = module.ingest_workflow.state_machine_arn
}

output "ingest_queue_url" {
  description = "Ingest queue (stream, reconciler, control API)."
  value       = module.ingest_queue.queue_url
}

output "ingest_dlq_url" {
  description = "Ingest queue DLQ."
  value       = module.ingest_queue.dlq_url
}

output "backfill_queue_url" {
  description = "Low-priority backfill queue."
  value       = module.backfill_queue.queue_url
}

output "pipe_dlq_url" {
  description = "Stream records the Pipe couldn't deliver."
  value       = aws_sqs_queue.pipe_dlq.url
}

output "status_table_name" {
  description = "ingestion_status table."
  value       = module.status_table.name
}

output "cache_table_name" {
  description = "embedding_cache table."
  value       = module.cache_table.name
}

output "staging_bucket" {
  description = "Staging bucket."
  value       = module.staging.id
}

output "database_mode" {
  description = "aurora (Aurora + RDS Proxy) or rds (single instance)."
  value       = var.database_mode
}

output "db_endpoint" {
  description = "Where the index function connects (RDS Proxy, or the RDS instance)."
  value       = local.db_host
}

output "db_writer_endpoint" {
  description = "Writer endpoint for migrations (master user)."
  value       = local.db_direct_host
}

output "migrate_function_name" {
  description = "Invoke after apply: aws lambda invoke --function-name <this> /dev/stdout"
  value       = module.fn["migrate"].function_name
}

output "tables_function_name" {
  description = "Vector-table status and switch as ingest_ops (the control API's /v1/tables routes call it)."
  value       = module.fn["tables"].function_name
}

output "alert_topic_arn" {
  description = "Permanent-failure alerts."
  value       = module.alerts.topic_arn
}

output "function_names" {
  description = "All function names by role."
  value       = { for k, f in module.fn : k => f.function_name }
}

output "control_api_url" {
  description = "Control API base URL (IAM auth; sign requests with SigV4)."
  value       = module.control_api.invoke_url
}

output "control_api_operator_policy_arn" {
  description = "IAM policy for operators: every control API route."
  value       = module.control_api.invoke_policy_arn
}

output "control_api_reader_policy_arn" {
  description = "IAM policy for readers: GET routes only."
  value       = aws_iam_policy.control_reader.arn
}

output "control_api_search_policy_arn" {
  description = "IAM policy for search clients: POST /v1/search only (the tenant is checked against the producer's source access policy)."
  value       = aws_iam_policy.control_search.arn
}

output "query_function_name" {
  description = "Read-only pgvector access inside the VPC (search_hybrid, evaluation, drift check)."
  value       = module.fn["query"].function_name
}

output "reconciler_function_name" {
  description = "Reconciler Lambda (also runnable on demand)."
  value       = module.fn["reconciler"].function_name
}

output "ssm_parameter_names" {
  description = "SSM parameters published for other stacks (the ui stack)."
  value       = { for k, p in aws_ssm_parameter.published : k => p.name }
}

output "eval_function_name" {
  description = "Retrieval evaluation (golden set: staging _eval/golden/LATEST; reports: _eval/<table>/)."
  value       = module.fn["eval"].function_name
}
