# WAF in front of the API (required). Bring your own ACL with waf_acl_arn, or the stack creates one.

module "waf" {
  source = "../../modules/waf_web_acl"
  count  = var.waf_acl_arn == null ? 1 : 0

  name               = local.name_prefix
  resource_arns      = { api = module.api.stage_arn }
  rate_limit         = var.waf_rate_limit
  log_retention_days = var.log_retention_days
  log_kms_key_arn    = module.kms.key_arn

  managed_rule_groups = [
    { name = "AWSManagedRulesAmazonIpReputationList", priority = 10 },
    { name = "AWSManagedRulesKnownBadInputsRuleSet", priority = 20 },
    {
      name     = "AWSManagedRulesCommonRuleSet"
      priority = 30
      # Bodies can legitimately exceed 8 KB (up to 50 metadata pairs of 2 KB each); the Lambda
      # validates every body, so this rule only counts.
      count_rules = ["SizeRestrictions_BODY"]
    },
  ]
}

resource "aws_wafv2_web_acl_association" "external" {
  count = var.waf_acl_arn == null ? 0 : 1

  resource_arn = module.api.stage_arn
  web_acl_arn  = var.waf_acl_arn
}
