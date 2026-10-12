mock_provider "aws" {
  override_during = plan

  mock_data "aws_availability_zones" {
    defaults = { names = ["us-west-2a", "us-west-2b", "us-west-2c", "us-west-2d"] }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-west-2" }
  }
}

variables {
  name        = "test"
  kms_key_arn = "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000"
}

run "private_only" {
  command = plan

  assert {
    condition     = length(aws_subnet.private) == 3 && alltrue([for s in aws_subnet.private : !s.map_public_ip_on_launch])
    error_message = "Expected 3 private subnets that never assign public IPs."
  }

  assert {
    condition     = [for s in aws_subnet.private : s.cidr_block] == ["10.40.0.0/24", "10.40.1.0/24", "10.40.2.0/24"]
    error_message = "Subnets must be carved from the VPC CIDR."
  }

  assert {
    condition     = aws_vpc.this.enable_dns_support && aws_vpc.this.enable_dns_hostnames
    error_message = "DNS support/hostnames are required for interface endpoints."
  }

  assert {
    condition     = aws_flow_log.this.traffic_type == "ALL" && aws_cloudwatch_log_group.flow.kms_key_id == var.kms_key_arn
    error_message = "Flow logs must capture ALL traffic into an encrypted log group."
  }

  assert {
    condition = (
      anytrue([for c in one(data.aws_iam_policy_document.flow_assume.statement).condition : c.variable == "aws:SourceAccount" && c.values == tolist(["123456789012"])])
      && anytrue([for c in one(data.aws_iam_policy_document.flow_assume.statement).condition : c.variable == "aws:SourceArn" && c.values == tolist(["arn:aws:ec2:us-west-2:123456789012:vpc-flow-log/*"])])
    )
    error_message = "Only flow logs in this account and region may assume the flow-logs role."
  }
}

run "rejects_single_az" {
  command = plan

  variables {
    az_count = 1
  }

  expect_failures = [var.az_count]
}

run "roles_carry_the_permissions_boundary" {
  command = plan

  variables {
    permissions_boundary = "arn:aws:iam::123456789012:policy/entingestion-dev-workload-boundary"
  }

  assert {
    condition     = aws_iam_role.flow.permissions_boundary == var.permissions_boundary && output.role_permissions_boundaries == [var.permissions_boundary]
    error_message = "Every role the module creates must carry the given permissions boundary."
  }
}
