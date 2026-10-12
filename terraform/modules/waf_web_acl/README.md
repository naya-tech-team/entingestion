# waf_web_acl

A regional AWS WAF web ACL. It contains:
- AWS managed rule groups. The defaults are IP reputation, known bad inputs and the common rule set.
- An optional per-IP rate limit.
- Associations with the regional resources you pass in.
- Logging to an encrypted `aws-waf-logs-*` log group, with the `authorization` and session-token headers redacted.

Use `count_rules` to switch individual managed rules to COUNT where they would block legitimate traffic. For example, `SizeRestrictions_BODY` in the common rule set blocks request bodies over 8 KB.

```hcl
module "waf" {
  source          = "../../modules/waf_web_acl"
  name            = "producer-dev"
  resource_arns   = { api = module.api.stage_arn }
  log_kms_key_arn = module.kms.key_arn
  managed_rule_groups = [
    { name = "AWSManagedRulesAmazonIpReputationList", priority = 10 },
    { name = "AWSManagedRulesKnownBadInputsRuleSet", priority = 20 },
    { name = "AWSManagedRulesCommonRuleSet", priority = 30, count_rules = ["SizeRestrictions_BODY"] },
  ]
}
```

| Output | Description |
|---|---|
| `web_acl_arn`, `web_acl_name` | The ACL |
| `log_group_name` | WAF logs |
| `settings` | Effective settings, for plan assertions |
