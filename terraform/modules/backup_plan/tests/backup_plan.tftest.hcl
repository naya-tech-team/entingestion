mock_provider "aws" {
  override_during = plan

  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
}

mock_provider "aws" {
  alias           = "copy"
  override_during = plan

  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-2:123456789012:key/copy" }
  }
  mock_resource "aws_backup_vault" {
    defaults = { arn = "arn:aws:backup:us-east-2:123456789012:backup-vault:svc-prod-backup-copy" }
  }
}

variables {
  name                 = "svc-prod-backup"
  kms_key_arn          = "arn:aws:kms:us-west-2:123456789012:key/primary"
  resource_arns        = ["arn:aws:dynamodb:us-west-2:123456789012:table/articles", "arn:aws:rds:us-west-2:123456789012:cluster:svc-prod"]
  permissions_boundary = "arn:aws:iam::123456789012:policy/entingestion-prod-workload-boundary"
}

run "daily_backups_copied_to_another_region" {
  command = plan

  assert {
    condition = (
      one(aws_backup_plan.this.rule).schedule == "cron(0 5 * * ? *)"
      && one(one(aws_backup_plan.this.rule).lifecycle).delete_after == 35
      && one(one(one(aws_backup_plan.this.rule).copy_action).lifecycle).delete_after == 35
      && one(one(aws_backup_plan.this.rule).copy_action).destination_vault_arn == "arn:aws:backup:us-east-2:123456789012:backup-vault:svc-prod-backup-copy"
    )
    error_message = "Daily recovery points, kept 35 days here and in the copy region."
  }

  assert {
    condition     = aws_backup_vault.this.kms_key_arn == var.kms_key_arn && aws_backup_vault.copy.kms_key_arn == "arn:aws:kms:us-east-2:123456789012:key/copy"
    error_message = "Both vaults are encrypted with customer-managed keys of their region."
  }

  assert {
    condition     = toset(aws_backup_selection.this.resources) == toset(var.resource_arns)
    error_message = "The selection backs up the given resources."
  }

  assert {
    condition     = aws_iam_role.this.permissions_boundary == var.permissions_boundary && output.role_permissions_boundaries == [var.permissions_boundary]
    error_message = "The backup role carries the workload boundary."
  }
}

run "rejects_short_retention" {
  command = plan

  variables {
    retention_days = 1
  }

  expect_failures = [var.retention_days]
}
