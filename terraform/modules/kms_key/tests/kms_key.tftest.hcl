mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-west-2" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
}

variables {
  alias       = "test-key"
  description = "test"
}

run "rotation_and_alias" {
  command = plan

  assert {
    condition     = aws_kms_key.this.enable_key_rotation
    error_message = "Key rotation must be enabled."
  }

  assert {
    condition     = aws_kms_alias.this.name == "alias/test-key"
    error_message = "Alias must be prefixed with alias/."
  }
}

run "extra_statements_are_appended" {
  command = plan

  variables {
    policy_statements = [{
      sid        = "CloudWatchLogs"
      actions    = ["kms:Encrypt*", "kms:Decrypt*"]
      principals = [{ type = "Service", identifiers = ["logs.us-west-2.amazonaws.com"] }]
    }]
  }

  assert {
    condition     = length(data.aws_iam_policy_document.key.statement) == 2
    error_message = "Expected the root statement plus one extra statement."
  }
}

run "no_service_statements_by_default" {
  command = plan

  assert {
    condition     = output.statement_sids == ["AccountRootFullAccess"]
    error_message = "Without the flags the key policy has only the account statement."
  }
}

run "cloudwatch_statements_come_first_and_are_scoped" {
  command = plan

  variables {
    allow_cloudwatch_logs         = true
    allow_cloudwatch_alarms_topic = true
    policy_statements = [{
      sid        = "Extra"
      actions    = ["kms:Decrypt"]
      principals = [{ type = "Service", identifiers = ["example.amazonaws.com"] }]
    }]
  }

  assert {
    condition     = output.statement_sids == ["AccountRootFullAccess", "CloudWatchLogsEncryption", "CloudWatchAlarmsToEncryptedTopic", "Extra"]
    error_message = "The CloudWatch statements follow the account statement, before policy_statements."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.key.statement :
      s.sid == "CloudWatchLogsEncryption"
      && anytrue([for p in s.principals : p.identifiers == toset(["logs.us-west-2.amazonaws.com"])])
      && anytrue([for c in s.condition : c.test == "ArnLike" && c.variable == "kms:EncryptionContext:aws:logs:arn" && c.values == tolist(["arn:aws:logs:us-west-2:123456789012:log-group:*"])])
    ])
    error_message = "CloudWatch Logs may use the key only for this account's log groups in this region."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.key.statement :
      s.sid == "CloudWatchAlarmsToEncryptedTopic"
      && s.actions == toset(["kms:Decrypt", "kms:GenerateDataKey*"])
      && anytrue([for p in s.principals : p.identifiers == toset(["cloudwatch.amazonaws.com"])])
      && anytrue([for c in s.condition : c.test == "StringEquals" && c.variable == "aws:SourceAccount" && c.values == tolist(["123456789012"])])
    ])
    error_message = "CloudWatch alarms may use the key only for this account."
  }
}

run "account_root_manages_the_key_by_default" {
  command = plan

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.key.statement :
      s.sid == "AccountRootFullAccess" && s.actions == toset(["kms:*"])
      && anytrue([for p in s.principals : p.identifiers == toset(["arn:aws:iam::123456789012:root"])])
    ])
    error_message = "Without admin_principals the key delegates everything to IAM (unchanged default)."
  }
}

run "admin_principals_split_administration_from_use" {
  command = plan

  variables {
    admin_principals = ["arn:aws:iam::123456789012:role/entingestion-dev-deploy"]
  }

  assert {
    condition     = !anytrue([for s in data.aws_iam_policy_document.key.statement : contains(s.actions, "kms:*")])
    error_message = "Nobody keeps kms:* once administrators are named."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.key.statement :
      s.sid == "KeyAdministrators" && contains(s.actions, "kms:Put*") && contains(s.actions, "kms:ScheduleKeyDeletion")
      && !contains(s.actions, "kms:Decrypt")
      && anytrue([for p in s.principals : p.identifiers == toset(var.admin_principals)])
    ])
    error_message = "Only the named administrators manage the key, and they can't decrypt with it."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.key.statement :
      s.sid == "AccountRootUse" && contains(s.actions, "kms:Decrypt") && !contains(s.actions, "kms:Put*")
    ])
    error_message = "IAM still decides who may use the key."
  }
}

run "rejects_non_iam_admins" {
  command = plan

  variables {
    admin_principals = ["*"]
  }

  expect_failures = [var.admin_principals]
}

run "rejects_aws_reserved_alias" {
  command = plan

  variables {
    alias = "aws/s3"
  }

  expect_failures = [var.alias]
}
