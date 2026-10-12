mock_provider "aws" {
  override_during = plan

  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-west-2" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_resource "aws_db_proxy" {
    defaults = { arn = "arn:aws:rds:us-west-2:123456789012:db-proxy:prx-0abc123" }
  }
}

variables {
  name                       = "proxy"
  vpc_id                     = "vpc-123"
  subnet_ids                 = ["subnet-1", "subnet-2"]
  cluster_id                 = "db"
  cluster_resource_id        = "cluster-XYZ"
  db_security_group_id       = "sg-db"
  allowed_security_group_ids = { index = "sg-index" }
}

run "end_to_end_iam_and_tls" {
  command = plan

  assert {
    condition     = aws_db_proxy.this.require_tls && aws_db_proxy.this.default_auth_scheme == "IAM_AUTH"
    error_message = "Proxy must require TLS and use end-to-end IAM auth."
  }

  assert {
    condition = one(data.aws_iam_policy_document.connect.statement).resources == toset([
      "arn:aws:rds-db:us-west-2:123456789012:dbuser:cluster-XYZ/*",
    ])
    error_message = "Proxy role may only connect to its cluster."
  }

  assert {
    condition = (
      output.proxy_resource_id == "prx-0abc123"
      && output.connect_arn_prefix == "arn:aws:rds-db:us-west-2:123456789012:dbuser:prx-0abc123/"
    )
    error_message = "Client connect ARN must use the proxy resource id."
  }

  assert {
    condition = (
      aws_vpc_security_group_ingress_rule.clients["index"].referenced_security_group_id == "sg-index"
      && aws_vpc_security_group_ingress_rule.db_from_proxy.security_group_id == "sg-db"
    )
    error_message = "Clients -> proxy and proxy -> cluster must be the only paths."
  }
}

run "roles_carry_the_permissions_boundary" {
  command = plan

  variables {
    permissions_boundary = "arn:aws:iam::123456789012:policy/entingestion-dev-workload-boundary"
  }

  assert {
    condition     = aws_iam_role.this.permissions_boundary == var.permissions_boundary && output.role_permissions_boundaries == [var.permissions_boundary]
    error_message = "Every role the module creates must carry the given permissions boundary."
  }
}
