mock_provider "aws" {}

variables {
  name        = "test"
  description = "test"
  vpc_id      = "vpc-123"
}

run "deny_by_default" {
  command = plan

  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.this) == 0 && length(aws_vpc_security_group_egress_rule.this) == 0
    error_message = "No rules unless given."
  }
}

run "rules_are_created_per_key" {
  command = plan

  variables {
    ingress = { app = { description = "app", from_port = 5432, to_port = 5432, referenced_security_group_id = "sg-app" } }
    egress  = { s3 = { description = "s3", from_port = 443, to_port = 443, prefix_list_id = "pl-123" } }
  }

  assert {
    condition = (
      aws_vpc_security_group_ingress_rule.this["app"].referenced_security_group_id == "sg-app"
      && aws_vpc_security_group_egress_rule.this["s3"].prefix_list_id == "pl-123"
    )
    error_message = "Rules must be passed through."
  }
}

run "rejects_rule_with_two_sources" {
  command = plan

  variables {
    ingress = { bad = { description = "bad", from_port = 1, to_port = 1, cidr_ipv4 = "10.0.0.0/8", prefix_list_id = "pl-1" } }
  }

  expect_failures = [var.ingress]
}
