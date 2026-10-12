# Values the ui stack reads, so it doesn't depend on this stack's Terraform state.
locals {
  published_parameters = {
    # The upload UI's BFF calls the control API (stage invoke URL, no trailing slash).
    control_api_url           = trimsuffix(module.control_api.invoke_url, "/")
    control_api_execution_arn = module.control_api.execution_arn
  }
}

resource "aws_ssm_parameter" "published" {
  #checkov:skip=CKV2_AWS_34:Values are non-secret resource identifiers; plain String keeps consumers free of KMS grants.
  for_each = local.published_parameters

  name        = "${var.ssm_prefix}/${var.env}/ingestion/${each.key}"
  description = "Ingestion ${var.env}: ${each.key} (managed by Terraform)"
  type        = "String"
  value       = each.value
}
