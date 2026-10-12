mock_provider "aws" {
  override_during = plan

  mock_resource "aws_wafv2_web_acl" {
    defaults = { arn = "arn:aws:wafv2:us-west-2:123456789012:regional/webacl/test/abc" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-west-2:123456789012:log-group:aws-waf-logs-test" }
  }
}

variables {
  name            = "test"
  resource_arns   = { api = "arn:aws:apigateway:us-west-2::/restapis/abc/stages/prod" }
  log_kms_key_arn = "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000"
}

run "defaults" {
  command = plan

  assert {
    condition     = aws_wafv2_web_acl.this.scope == "REGIONAL" && length(aws_wafv2_web_acl.this.default_action[0].allow) == 1
    error_message = "ACL must be REGIONAL and allow by default."
  }

  assert {
    condition     = length(aws_wafv2_web_acl.this.rule) == 4
    error_message = "Expected 3 managed rule groups plus the rate rule."
  }

  assert {
    condition     = aws_wafv2_web_acl_association.this["api"].resource_arn == var.resource_arns.api
    error_message = "ACL must be associated with each given resource."
  }

  assert {
    condition     = startswith(aws_cloudwatch_log_group.this.name, "aws-waf-logs-")
    error_message = "WAF log group must be named aws-waf-logs-*."
  }

  assert {
    condition     = length(aws_wafv2_web_acl_logging_configuration.this.redacted_fields) == 2
    error_message = "Credential headers must be redacted from WAF logs."
  }
}

run "count_overrides_and_no_rate_rule" {
  command = plan

  variables {
    rate_limit = null
    managed_rule_groups = [
      { name = "AWSManagedRulesCommonRuleSet", priority = 30, count_rules = ["SizeRestrictions_BODY"] },
    ]
  }

  assert {
    condition     = length(aws_wafv2_web_acl.this.rule) == 1
    error_message = "Only the managed group should remain without a rate limit."
  }

  assert {
    condition = one([
      for r in aws_wafv2_web_acl.this.rule :
      r.statement[0].managed_rule_group_statement[0].rule_action_override[0].name
    ]) == "SizeRestrictions_BODY"
    error_message = "count_rules must become rule_action_override COUNT."
  }
}

run "rejects_low_rate_limit" {
  command = plan

  variables {
    rate_limit = 10
  }

  expect_failures = [var.rate_limit]
}
