mock_provider "aws" {
  override_during = plan

  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
}

variables {
  name                       = "db"
  database_name              = "ingest"
  vpc_id                     = "vpc-123"
  subnet_ids                 = ["subnet-1", "subnet-2"]
  allowed_security_group_ids = { proxy = "sg-proxy" }
  kms_key_arn                = "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000"
}

run "secure_serverless_cluster" {
  command = plan

  assert {
    condition = (
      aws_rds_cluster.this.storage_encrypted
      && aws_rds_cluster.this.kms_key_id == var.kms_key_arn
      && aws_rds_cluster.this.iam_database_authentication_enabled
      && aws_rds_cluster.this.manage_master_user_password
      && aws_rds_cluster.this.deletion_protection
    )
    error_message = "Cluster must be encrypted, IAM-auth enabled, with a managed master secret and deletion protection."
  }

  assert {
    condition = (
      one(aws_rds_cluster.this.serverlessv2_scaling_configuration).min_capacity == 0.5
      && aws_rds_cluster_instance.this[0].instance_class == "db.serverless"
      && aws_rds_cluster_instance.this[0].performance_insights_enabled
    )
    error_message = "Instances must be Serverless v2 with Performance Insights."
  }

  assert {
    condition     = aws_rds_cluster_parameter_group.this.family == "aurora-postgresql16"
    error_message = "Parameter group family must follow the engine major version."
  }

  assert {
    condition     = aws_vpc_security_group_ingress_rule.clients["proxy"].referenced_security_group_id == "sg-proxy"
    error_message = "Only the allowed groups may reach 5432."
  }

  assert {
    condition     = aws_rds_cluster.this.final_snapshot_identifier == "db-final"
    error_message = "A final snapshot must be taken unless skipped."
  }
}

run "rejects_single_subnet" {
  command = plan

  variables {
    subnet_ids = ["subnet-1"]
  }

  expect_failures = [var.subnet_ids]
}

run "roles_carry_the_permissions_boundary" {
  command = plan

  variables {
    monitoring_interval  = 60
    permissions_boundary = "arn:aws:iam::123456789012:policy/entingestion-dev-workload-boundary"
  }

  assert {
    condition     = aws_iam_role.monitoring[0].permissions_boundary == var.permissions_boundary && output.role_permissions_boundaries == [var.permissions_boundary]
    error_message = "Every role the module creates must carry the given permissions boundary."
  }
}
