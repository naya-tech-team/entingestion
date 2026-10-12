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
  mock_resource "aws_db_instance" {
    defaults = {
      arn                = "arn:aws:rds:us-west-2:123456789012:db:db"
      resource_id        = "db-ABC"
      master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:us-west-2:123456789012:secret:rds" }]
    }
  }
}

variables {
  name                       = "db"
  database_name              = "ingest"
  vpc_id                     = "vpc-123"
  subnet_ids                 = ["subnet-1", "subnet-2"]
  allowed_security_group_ids = { index = "sg-index" }
  kms_key_arn                = "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000"
}

run "private_encrypted_iam_auth" {
  command = plan

  assert {
    condition = (
      !aws_db_instance.this.publicly_accessible
      && aws_db_instance.this.storage_encrypted
      && aws_db_instance.this.kms_key_id == var.kms_key_arn
      && aws_db_instance.this.iam_database_authentication_enabled
      && aws_db_instance.this.manage_master_user_password
    )
    error_message = "Instance must be private, CMK-encrypted, IAM-auth enabled, with a managed master secret."
  }

  assert {
    condition     = aws_db_parameter_group.this.family == "postgres17"
    error_message = "Parameter group family must follow the engine major version."
  }

  assert {
    condition     = output.connect_arn_prefix == "arn:aws:rds-db:us-west-2:123456789012:dbuser:db-ABC/"
    error_message = "IAM connect ARN must use the instance resource id."
  }
}

run "free_plan_settings" {
  command = plan

  variables {
    backup_retention_days = 1
    performance_insights  = false
    monitoring_interval   = 0
    deletion_protection   = false
    skip_final_snapshot   = true
  }

  assert {
    condition = (
      aws_db_instance.this.backup_retention_period == 1
      && !aws_db_instance.this.performance_insights_enabled
      && length(aws_iam_role.monitoring) == 0
      && aws_db_instance.this.instance_class == "db.t4g.micro"
    )
    error_message = "Free-plan settings must be applied."
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
