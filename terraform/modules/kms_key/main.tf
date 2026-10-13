data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  partition    = data.aws_partition.current.partition
  region       = data.aws_region.current.region
  account_root = "arn:${local.partition}:iam::${local.account_id}:root"
  split_admin  = length(var.admin_principals) > 0
  admin_actions = [
    "kms:Create*", "kms:Describe*", "kms:Enable*", "kms:List*", "kms:Put*", "kms:Update*",
    "kms:Revoke*", "kms:Disable*", "kms:Get*", "kms:Delete*", "kms:TagResource",
    "kms:UntagResource", "kms:ScheduleKeyDeletion", "kms:CancelKeyDeletion",
  ]

  # Service principals that act without an IAM role, before the caller's own statements.
  service_statements = concat(
    var.allow_cloudwatch_logs ? [{
      # CloudWatch Logs encrypts this account's log groups in this region with the key.
      sid        = "CloudWatchLogsEncryption"
      actions    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
      principals = [{ type = "Service", identifiers = ["logs.${local.region}.amazonaws.com"] }]
      conditions = [{
        test     = "ArnLike"
        variable = "kms:EncryptionContext:aws:logs:arn"
        values   = ["arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:*"]
      }]
    }] : [],
    var.allow_cloudwatch_alarms_topic ? [{
      # CloudWatch alarms of this account publish to an SNS topic encrypted with the key.
      sid        = "CloudWatchAlarmsToEncryptedTopic"
      actions    = ["kms:Decrypt", "kms:GenerateDataKey*"]
      principals = [{ type = "Service", identifiers = ["cloudwatch.amazonaws.com"] }]
      conditions = [{
        test     = "StringEquals"
        variable = "aws:SourceAccount"
        values   = [local.account_id]
      }]
    }] : [],
  )
}

data "aws_iam_policy_document" "key" {
  # In a key policy, resources = ["*"] means "this key"; it can't grant access to anything else.
  #checkov:skip=CKV_AWS_109:Key policy resource "*" is scoped to this key by definition.
  #checkov:skip=CKV_AWS_111:Key policy resource "*" is scoped to this key by definition.
  #checkov:skip=CKV_AWS_356:Key policy resource "*" is scoped to this key by definition.

  # Default: delegate all key access to IAM; identity policies in this account decide who can
  # use and manage the key.
  dynamic "statement" {
    for_each = local.split_admin ? [] : [1]

    content {
      sid       = "AccountRootFullAccess"
      actions   = ["kms:*"]
      resources = ["*"]

      principals {
        type        = "AWS"
        identifiers = [local.account_root]
      }
    }
  }

  # With admin_principals: only they manage the key; IAM still decides who may use it.
  dynamic "statement" {
    for_each = local.split_admin ? [1] : []

    content {
      sid       = "KeyAdministrators"
      actions   = local.admin_actions
      resources = ["*"]

      principals {
        type        = "AWS"
        identifiers = var.admin_principals
      }
    }
  }

  dynamic "statement" {
    for_each = local.split_admin ? [1] : []

    content {
      sid       = "AccountRootUse"
      actions   = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"]
      resources = ["*"]

      principals {
        type        = "AWS"
        identifiers = [local.account_root]
      }
    }
  }

  # AWS services (RDS, Lambda, ...) create grants on the caller's behalf.
  dynamic "statement" {
    for_each = local.split_admin ? [1] : []

    content {
      sid       = "AccountRootGrantsForAwsResources"
      actions   = ["kms:CreateGrant", "kms:ListGrants", "kms:RevokeGrant"]
      resources = ["*"]

      principals {
        type        = "AWS"
        identifiers = [local.account_root]
      }

      condition {
        test     = "Bool"
        variable = "kms:GrantIsForAWSResource"
        values   = ["true"]
      }
    }
  }

  dynamic "statement" {
    for_each = concat(local.service_statements, var.policy_statements)

    content {
      sid       = statement.value.sid
      actions   = statement.value.actions
      resources = ["*"]

      dynamic "principals" {
        for_each = statement.value.principals

        content {
          type        = principals.value.type
          identifiers = principals.value.identifiers
        }
      }

      dynamic "condition" {
        for_each = statement.value.conditions

        content {
          test     = condition.value.test
          variable = condition.value.variable
          values   = condition.value.values
        }
      }
    }
  }
}

resource "aws_kms_key" "this" {
  description             = var.description
  enable_key_rotation     = true
  deletion_window_in_days = var.deletion_window_in_days
  policy                  = data.aws_iam_policy_document.key.json
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.alias}"
  target_key_id = aws_kms_key.this.key_id
}
