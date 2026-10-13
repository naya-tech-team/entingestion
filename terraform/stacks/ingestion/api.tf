# Control API: REST + IAM auth. Three invoke policies: operators (every route),
# readers (GET only) and searchers (POST /v1/search only). Attach them to the roles or users
# that need them. Search also checks the caller against the producer's source access policy.

module "control_api" {
  source = "../../modules/rest_api_lambda_proxy"

  name                  = "${local.name_prefix}-control"
  description           = "Ingestion control API (${var.env})"
  lambda_function_name  = module.fn["control_api"].function_name
  lambda_invoke_arn     = module.fn["control_api"].alias_invoke_arn
  lambda_qualifier      = module.fn["control_api"].alias_name
  stage_name            = "api"
  throttle_rate_limit   = var.api_throttle_rate
  throttle_burst_limit  = var.api_throttle_burst
  log_retention_days    = var.log_retention_days
  log_kms_key_arn       = module.kms.key_arn
  invoke_policy_name    = "${local.name_prefix}-control-operator"
  invoke_resource_paths = ["*/v1/*"]
  permissions_boundary  = local.permissions_boundary
}

data "aws_iam_policy_document" "control_reader" {
  statement {
    sid       = "ReadOnlyRoutes"
    actions   = ["execute-api:Invoke"]
    resources = ["${module.control_api.execution_arn}/${module.control_api.stage_name}/GET/v1/*"]
  }
}

resource "aws_iam_policy" "control_reader" {
  name        = "${local.name_prefix}-control-reader"
  description = "Read-only access to the ingestion control API (${var.env}): status and summaries."
  policy      = data.aws_iam_policy_document.control_reader.json
}

data "aws_iam_policy_document" "control_search" {
  statement {
    sid       = "SearchRoute"
    actions   = ["execute-api:Invoke"]
    resources = ["${module.control_api.execution_arn}/${module.control_api.stage_name}/POST/v1/search"]
  }
}

resource "aws_iam_policy" "control_search" {
  name        = "${local.name_prefix}-control-search"
  description = "Search access to the ingestion control API (${var.env}): POST /v1/search only; tenants per the producer's source access policy."
  policy      = data.aws_iam_policy_document.control_search.json
}

# WAF in front of the API. Bring your own ACL with waf_acl_arn, or the stack creates one.
module "waf" {
  source = "../../modules/waf_web_acl"
  count  = var.waf_acl_arn == null ? 1 : 0

  name               = "${local.name_prefix}-control"
  resource_arns      = { api = module.control_api.stage_arn }
  rate_limit         = var.waf_rate_limit
  log_retention_days = var.log_retention_days
  log_kms_key_arn    = module.kms.key_arn

  managed_rule_groups = [
    { name = "AWSManagedRulesAmazonIpReputationList", priority = 10 },
    { name = "AWSManagedRulesKnownBadInputsRuleSet", priority = 20 },
    { name = "AWSManagedRulesCommonRuleSet", priority = 30 },
  ]
}

resource "aws_wafv2_web_acl_association" "external" {
  count = var.waf_acl_arn == null ? 0 : 1

  resource_arn = module.control_api.stage_arn
  web_acl_arn  = var.waf_acl_arn
}
