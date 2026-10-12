# Values the ingestion and ui stacks read, so they don't depend on this stack's Terraform state.
locals {
  published_parameters = {
    articles_stream_arn = module.articles_table.stream_arn
    articles_table_name = module.articles_table.name
    process_table_name  = module.process_table.name
    raw_bucket_name     = module.raw_bucket.id
    kms_key_arn         = module.kms.key_arn
    source_access       = local.source_access_json # ingestion's search authorizes with it
    # The upload UI's BFF calls the API (stage invoke URL, no trailing slash) and may invoke it.
    api_url           = trimsuffix(module.api.invoke_url, "/")
    api_execution_arn = module.api.execution_arn
  }
}

resource "aws_ssm_parameter" "published" {
  #checkov:skip=CKV2_AWS_34:Values are non-secret resource identifiers; plain String keeps consumers free of KMS grants.
  for_each = local.published_parameters

  name        = "${var.ssm_prefix}/${var.env}/producer/${each.key}"
  description = "Producer ${var.env}: ${each.key} (managed by Terraform)"
  type        = "String"
  value       = each.value
}
