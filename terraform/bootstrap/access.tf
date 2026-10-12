# Who may run Terraform, and as what (docs/AWS_ACCESS.md).
#
# People sign in to the console with MFA and get short-lived CLI credentials with `aws login`;
# Terraform then runs as entingestion-<env>-deploy (one role per environment). No long-lived
# access keys. A GitHub Actions trust is built in but off until github_repository is set.
#
# Each role is AdministratorAccess inside a permissions boundary that denies: minting long-lived
# credentials, changing this access layer, destroying or opening the state store and audit trail,
# account-level changes, touching the other environments' state and resources, and creating or
# changing roles outside its own environment or without the workload boundary. Every role the
# stacks create carries that workload boundary (no IAM at all), so the deploy role can't mint
# itself an unbounded role. dev and prod share this account, so these guard against accidents;
# the hard boundary is separate accounts (phase 2).

locals {
  github_oidc_host = "token.actions.githubusercontent.com"
  state_bucket_arn = "arn:${local.partition}:s3:::entingestion-tfstate-${var.aws_account_id}-${var.region}"
  deploy_role_arns = { for env in var.environments : env => "arn:${local.partition}:iam::${var.aws_account_id}:role/entingestion-${env}-deploy" }
  partition        = data.aws_partition.current.partition
  iam_arn          = "arn:${local.partition}:iam::${var.aws_account_id}"

  # deploy_principal_arns may be relative to the account ("user/<name>"); derive the full ARN.
  deploy_principals = [for p in var.deploy_principal_arns : startswith(p, "arn:") ? p : "${local.iam_arn}:${p}"]

  # Who is applying, as the principal the state-bucket policy sees (an assumed-role session
  # counts as its role).
  caller_principal = replace(data.aws_caller_identity.current.arn,
  "/^arn:([^:]+):sts::([0-9]{12}):assumed-role/([^/]+)/.*$/", "arn:$1:iam::$2:role/$3")

  # Computed rather than referenced, so the deploy boundary doesn't wait on the policy resource.
  workload_boundary_arns = { for env in var.environments : env => "${local.iam_arn}:policy/entingestion-${env}-workload-boundary" }
  other_envs             = { for env in var.environments : env => sort(tolist(setsubtract(var.environments, [env]))) }

  # Resources of an environment by name, for services whose actions don't all support the env
  # tag. The stacks name things <stack>-<env>-* (functions, queues, topics, state machines,
  # buckets, log groups) and the tables <table_prefix><name>; prod's tables have no prefix.
  env_name_arns = {
    for env in var.environments : env => concat(
      [
        "arn:${local.partition}:lambda:*:*:function:*-${env}-*",
        "arn:${local.partition}:states:*:*:stateMachine:*-${env}-*",
        "arn:${local.partition}:states:*:*:execution:*-${env}-*",
        "arn:${local.partition}:sqs:*:*:*-${env}-*",
        "arn:${local.partition}:sns:*:*:*-${env}-*",
        "arn:${local.partition}:s3:::*-${env}-*",
        "arn:${local.partition}:logs:*:*:log-group:*-${env}-*",
        "arn:${local.partition}:logs:*:*:log-group:*-${env}/*",
        "arn:${local.partition}:ssm:*:*:parameter/entingestion/${env}/*",
        # CloudFront Functions are named ui-<env>-*; distributions, KeyValueStores and OACs have
        # generated ids, so the env tag guards them.
        "arn:${local.partition}:cloudfront::*:function/*-${env}-*",
      ],
      lookup(var.table_prefixes, env, "${env}-") != ""
      ? ["arn:${local.partition}:dynamodb:*:*:table/${lookup(var.table_prefixes, env, "${env}-")}*"]
      : flatten([for t in local.table_names : ["arn:${local.partition}:dynamodb:*:*:table/${t}", "arn:${local.partition}:dynamodb:*:*:table/${t}/*"]]),
    )
  }
  # The stacks' DynamoDB tables (producer: process, articles, producer_uploads; ingestion: the rest).
  table_names = ["process", "articles", "producer_uploads", "ingestion_status", "embedding_cache"]
}

data "aws_partition" "current" {}

data "aws_caller_identity" "current" {}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.github_repository == null ? 0 : 1

  url            = "https://${local.github_oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "deploy_trust" {
  for_each = toset(var.environments)

  statement {
    sid     = "People"
    actions = ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"]

    principals {
      type        = "AWS"
      identifiers = local.deploy_principals
    }

    dynamic "condition" {
      for_each = var.require_mfa ? [1] : []

      content {
        test     = "Bool"
        variable = "aws:MultiFactorAuthPresent"
        values   = ["true"]
      }
    }
  }

  dynamic "statement" {
    for_each = var.github_repository == null ? [] : [var.github_repository]

    content {
      sid     = "GitHubActions"
      actions = ["sts:AssumeRoleWithWebIdentity"]

      principals {
        type        = "Federated"
        identifiers = [aws_iam_openid_connect_provider.github[0].arn]
      }

      condition {
        test     = "StringEquals"
        variable = "${local.github_oidc_host}:aud"
        values   = ["sts.amazonaws.com"]
      }

      # Only jobs that run in the GitHub environment of the same name (with its reviewers).
      condition {
        test     = "StringEquals"
        variable = "${local.github_oidc_host}:sub"
        values   = ["repo:${statement.value}:environment:${each.key}"]
      }
    }
  }
}

data "aws_iam_policy_document" "deploy_boundary" {
  for_each = toset(var.environments)

  #checkov:skip=CKV_AWS_107:A permissions boundary only caps; the role's own policy grants.
  #checkov:skip=CKV_AWS_108:A permissions boundary only caps; the role's own policy grants.
  #checkov:skip=CKV_AWS_109:A permissions boundary only caps; the deny statements below restrict.
  #checkov:skip=CKV_AWS_110:A permissions boundary only caps; the deny statements below restrict.
  #checkov:skip=CKV_AWS_111:A permissions boundary only caps; the deny statements below restrict.
  #checkov:skip=CKV_AWS_356:A permissions boundary only caps; the deny statements below restrict.
  #checkov:skip=CKV_AWS_1:A permissions boundary only caps; the deny statements below restrict.
  #checkov:skip=CKV_AWS_283:A permissions boundary only caps; the deny statements below restrict.
  #checkov:skip=CKV_AWS_49:A permissions boundary only caps; the deny statements below restrict.
  statement {
    sid       = "UpToAdministrator"
    actions   = ["*"]
    resources = ["*"]
  }

  statement {
    sid    = "NoLongLivedCredentials"
    effect = "Deny"
    actions = [
      "iam:CreateUser",
      "iam:CreateAccessKey",
      "iam:UpdateAccessKey",
      "iam:CreateLoginProfile",
      "iam:UpdateLoginProfile",
      "iam:CreateServiceSpecificCredential",
      "iam:UploadSSHPublicKey",
      "iam:AttachUserPolicy",
      "iam:PutUserPolicy",
      "iam:AddUserToGroup",
    ]
    resources = ["*"]
  }

  # The deploy roles, every entingestion-* policy (both boundaries, the guardrails) and the OIDC provider.
  statement {
    sid     = "NoChangesToThisAccessLayer"
    effect  = "Deny"
    actions = ["iam:*"]
    resources = [
      "${local.iam_arn}:role/entingestion-*-deploy",
      "${local.iam_arn}:policy/entingestion-*",
      "${local.iam_arn}:oidc-provider/${local.github_oidc_host}",
    ]
  }

  # Roles the stacks create must carry this environment's workload boundary, keep it, and be
  # named like this environment's stacks; passing roles is limited to those too.
  statement {
    sid       = "RolesNeedTheWorkloadBoundary"
    effect    = "Deny"
    actions   = ["iam:CreateRole", "iam:PutRolePermissionsBoundary"]
    resources = ["*"]

    condition {
      test     = "StringNotEquals"
      variable = "iam:PermissionsBoundary"
      values   = [local.workload_boundary_arns[each.key]]
    }
  }

  statement {
    sid       = "KeepWorkloadBoundaries"
    effect    = "Deny"
    actions   = ["iam:DeleteRolePermissionsBoundary"]
    resources = ["*"]
  }

  statement {
    sid    = "OnlyThisEnvironmentsRoles"
    effect = "Deny"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:AttachRolePolicy",
      "iam:DetachRolePolicy",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:PutRolePermissionsBoundary",
      "iam:UpdateAssumeRolePolicy",
      "iam:UpdateRole",
      "iam:UpdateRoleDescription",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:PassRole",
    ]
    not_resources = [
      "${local.iam_arn}:role/producer-${each.key}-*",
      "${local.iam_arn}:role/ingestion-${each.key}-*",
      "${local.iam_arn}:role/ui-${each.key}-*",
    ]
  }

  # Customer managed policies: the stacks name theirs ingestion-<env>-*, producer-*-<env> and
  # ui-<env>-*.
  statement {
    sid    = "OnlyThisEnvironmentsPolicies"
    effect = "Deny"
    actions = [
      "iam:CreatePolicy",
      "iam:CreatePolicyVersion",
      "iam:DeletePolicy",
      "iam:DeletePolicyVersion",
      "iam:SetDefaultPolicyVersion",
    ]
    not_resources = [
      "${local.iam_arn}:policy/ingestion-${each.key}-*",
      "${local.iam_arn}:policy/producer-*-${each.key}",
      "${local.iam_arn}:policy/producer-${each.key}-*",
      "${local.iam_arn}:policy/ui-${each.key}-*",
    ]
  }

  statement {
    sid       = "NoPrivilegedManagedPolicies"
    effect    = "Deny"
    actions   = ["iam:AttachRolePolicy"]
    resources = ["*"]

    condition {
      test     = "ArnEquals"
      variable = "iam:PolicyARN"
      values = [
        "arn:${local.partition}:iam::aws:policy/AdministratorAccess",
        "arn:${local.partition}:iam::aws:policy/IAMFullAccess",
        "arn:${local.partition}:iam::aws:policy/PowerUserAccess",
      ]
    }
  }

  statement {
    sid    = "KeepTheStateStore"
    effect = "Deny"
    actions = [
      "s3:PutBucket*", # policy, versioning, public access block, ownership, ACL, tags
      "s3:DeleteBucket*",
      "s3:PutReplicationConfiguration",
      "s3:PutLifecycleConfiguration",
      "s3:PutEncryptionConfiguration",
    ]
    resources = [local.state_bucket_arn]
  }

  statement {
    sid       = "KeepTheStateKey"
    effect    = "Deny"
    actions   = ["kms:ScheduleKeyDeletion", "kms:DisableKey", "kms:PutKeyPolicy"]
    resources = [module.kms.key_arn]
  }

  # trail.tf: the trail, its bucket (and the logs in it) and its key.
  statement {
    sid    = "KeepTheAuditTrail"
    effect = "Deny"
    actions = [
      "cloudtrail:StopLogging",
      "cloudtrail:DeleteTrail",
      "cloudtrail:UpdateTrail",
      "cloudtrail:Put*",
      "s3:PutBucket*",
      "s3:DeleteBucket*",
      "s3:DeleteObject*",
      "s3:PutObject*",
      "s3:PutReplicationConfiguration",
      "s3:PutLifecycleConfiguration",
      "s3:PutEncryptionConfiguration",
      "kms:ScheduleKeyDeletion",
      "kms:DisableKey",
      "kms:PutKeyPolicy",
    ]
    resources = [local.trail_arn, module.trail_bucket.arn, "${module.trail_bucket.arn}/*", module.trail_kms.key_arn]
  }

  statement {
    sid       = "NoAccountChanges"
    effect    = "Deny"
    actions   = ["organizations:*", "account:*", "iam:CreateAccountAlias", "iam:DeleteAccountAlias", "iam:UpdateAccountPasswordPolicy"]
    resources = ["*"]
  }

  # Each environment's state is its own: <env>/<stack>/terraform.tfstate (+ .tflock). The
  # bootstrap's own state (bootstrap/) is for the people who apply it, not the deploy roles.
  statement {
    sid       = "OnlyOwnState"
    effect    = "Deny"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:GetObjectVersion", "s3:DeleteObjectVersion"]
    resources = [for prefix in concat(local.other_envs[each.key], ["bootstrap"]) : "${local.state_bucket_arn}/${prefix}/*"]
  }

  # Both stacks tag every resource with env (provider default_tags). Not every action supports
  # these condition keys, so this stops accidents, not a determined actor (see the header).
  dynamic "statement" {
    for_each = length(local.other_envs[each.key]) == 0 ? [] : [1]

    content {
      sid       = "NotOtherEnvironmentsResources"
      effect    = "Deny"
      actions   = ["*"]
      resources = ["*"]

      condition {
        test     = "StringEquals"
        variable = "aws:ResourceTag/env"
        values   = local.other_envs[each.key]
      }
    }
  }

  dynamic "statement" {
    for_each = length(local.other_envs[each.key]) == 0 ? [] : [1]

    content {
      sid       = "NotCreatingOtherEnvironmentsResources"
      effect    = "Deny"
      actions   = ["*"]
      resources = ["*"]

      condition {
        test     = "StringEquals"
        variable = "aws:RequestTag/env"
        values   = local.other_envs[each.key]
      }
    }
  }

  # The same by name, where the tag keys don't apply (for example S3 objects, SSM parameters).
  dynamic "statement" {
    for_each = length(local.other_envs[each.key]) == 0 ? [] : [1]

    content {
      sid       = "NotOtherEnvironmentsByName"
      effect    = "Deny"
      actions   = ["*"]
      resources = flatten([for other in local.other_envs[each.key] : local.env_name_arns[other]])
    }
  }
}

resource "aws_iam_policy" "deploy_boundary" {
  for_each = toset(var.environments)

  name        = "entingestion-${each.key}-deploy-boundary"
  description = "Caps entingestion-${each.key}-deploy: no long-lived credentials, access-layer, state, trail or account changes, bounded roles of this environment only, no other environments."
  policy      = data.aws_iam_policy_document.deploy_boundary[each.key].json
}

resource "aws_iam_role" "deploy" {
  for_each = toset(var.environments)

  name                 = "entingestion-${each.key}-deploy"
  description          = "Runs Terraform for the ${each.key} environment (make plan|apply ENV=${each.key})."
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust[each.key].json
  permissions_boundary = aws_iam_policy.deploy_boundary[each.key].arn
  max_session_duration = 3600
  tags                 = { env = each.key }
}

resource "aws_iam_role_policy_attachment" "deploy_admin" {
  for_each = toset(var.environments)

  #checkov:skip=CKV_AWS_274:Terraform manages IAM, VPC, RDS, Lambda and more; the permissions boundary caps it.
  role       = aws_iam_role.deploy[each.key].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AdministratorAccess"
}

# -- Workload boundary ----------------------------------------------------------------------
# Set on every role the stacks create (Lambda, Step Functions, Pipes, Scheduler, RDS, VPC flow
# logs). Workloads never manage IAM, so a role created by a deploy role can't be used to escape
# that deploy role's boundary. The role's own policies still grant (least privilege per role).

data "aws_iam_policy_document" "workload_boundary" {
  for_each = toset(var.environments)

  #checkov:skip=CKV_AWS_107:A permissions boundary only caps; the role's own policy grants.
  #checkov:skip=CKV_AWS_108:A permissions boundary only caps; the role's own policy grants.
  #checkov:skip=CKV_AWS_109:A permissions boundary only caps; the deny statements below restrict.
  #checkov:skip=CKV_AWS_110:A permissions boundary only caps; the deny statements below restrict.
  #checkov:skip=CKV_AWS_111:A permissions boundary only caps; the deny statements below restrict.
  #checkov:skip=CKV_AWS_356:A permissions boundary only caps; the deny statements below restrict.
  #checkov:skip=CKV_AWS_1:A permissions boundary only caps; the deny statements below restrict.
  #checkov:skip=CKV_AWS_283:A permissions boundary only caps; the deny statements below restrict.
  #checkov:skip=CKV_AWS_49:A permissions boundary only caps; the deny statements below restrict.
  statement {
    sid       = "UpToTheirOwnPolicies"
    actions   = ["*"]
    resources = ["*"]
  }

  statement {
    sid       = "NoIAM"
    effect    = "Deny"
    actions   = ["iam:*"]
    resources = ["*"]
  }

  statement {
    sid       = "NoAccountChanges"
    effect    = "Deny"
    actions   = ["organizations:*", "account:*"]
    resources = ["*"]
  }

  statement {
    sid       = "NoTrailChanges"
    effect    = "Deny"
    actions   = ["cloudtrail:StopLogging", "cloudtrail:DeleteTrail", "cloudtrail:UpdateTrail", "cloudtrail:Put*"]
    resources = ["*"]
  }

  statement {
    sid     = "NotTheStateOrTheTrail"
    effect  = "Deny"
    actions = ["*"]
    resources = [
      local.state_bucket_arn,
      "${local.state_bucket_arn}/*",
      module.kms.key_arn,
      module.trail_bucket.arn,
      "${module.trail_bucket.arn}/*",
      module.trail_kms.key_arn,
    ]
  }

  dynamic "statement" {
    for_each = length(local.other_envs[each.key]) == 0 ? [] : [1]

    content {
      sid       = "NotOtherEnvironmentsResources"
      effect    = "Deny"
      actions   = ["*"]
      resources = ["*"]

      condition {
        test     = "StringEquals"
        variable = "aws:ResourceTag/env"
        values   = local.other_envs[each.key]
      }
    }
  }

  dynamic "statement" {
    for_each = length(local.other_envs[each.key]) == 0 ? [] : [1]

    content {
      sid       = "NotOtherEnvironmentsByName"
      effect    = "Deny"
      actions   = ["*"]
      resources = flatten([for other in local.other_envs[each.key] : local.env_name_arns[other]])
    }
  }
}

resource "aws_iam_policy" "workload_boundary" {
  for_each = toset(var.environments)

  name        = "entingestion-${each.key}-workload-boundary"
  description = "Caps every role the ${each.key} stacks create: no IAM, account, trail or state access, no other environments."
  policy      = data.aws_iam_policy_document.workload_boundary[each.key].json
}

# The state bucket: only the deploy roles and the listed people (break-glass) may use it.
data "aws_iam_policy_document" "state_access" {
  # The policy denies the state bucket to everyone else, the admin applying it included: one who
  # isn't in deploy_principal_arns would lock themselves out of the bucket (only root, or a role
  # with a deploy role's name, could remove the policy again).
  lifecycle {
    precondition {
      condition     = contains(concat(local.deploy_principals, values(local.deploy_role_arns)), local.caller_principal) || endswith(local.caller_principal, ":root")
      error_message = "You (${local.caller_principal}) aren't in deploy_principal_arns: the state bucket policy would lock you out. Add yourself, e.g. TF_VAR_deploy_principal_arns='[\"user/<you>\"]'."
    }
  }

  statement {
    sid       = "OnlyDeployRolesAndAdmins"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [local.state_bucket_arn, "${local.state_bucket_arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringNotLike"
      variable = "aws:PrincipalArn"
      values = concat(
        values(local.deploy_role_arns),
        local.deploy_principals,
        ["arn:${local.partition}:iam::${var.aws_account_id}:root"],
      )
    }
  }

  # Nobody but the account root may copy the state elsewhere.
  statement {
    sid       = "NoReplicationExceptRoot"
    effect    = "Deny"
    actions   = ["s3:PutReplicationConfiguration"]
    resources = [local.state_bucket_arn]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringNotEquals"
      variable = "aws:PrincipalArn"
      values   = ["arn:${local.partition}:iam::${var.aws_account_id}:root"]
    }
  }

  # State objects are SSE-KMS (the backends set kms_key_id). A request may leave the header out
  # (the bucket default applies) but can't ask for anything else.
  statement {
    sid       = "OnlyKmsEncryptedState"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${local.state_bucket_arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringNotEqualsIfExists"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
  }
}
