mock_provider "aws" {
  override_during = plan

  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012", arn = "arn:aws:iam::123456789012:user/alice" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/entingestion-dev-deploy-boundary" }
  }
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::entingestion-trail-123456789012-us-west-2" }
  }
}

# Each boundary gets its own ARN, so a role pointing at the wrong environment's boundary fails.
override_resource {
  target          = aws_iam_policy.deploy_boundary["dev"]
  override_during = plan
  values          = { arn = "arn:aws:iam::123456789012:policy/entingestion-dev-deploy-boundary" }
}

override_resource {
  target          = aws_iam_policy.deploy_boundary["prod"]
  override_during = plan
  values          = { arn = "arn:aws:iam::123456789012:policy/entingestion-prod-deploy-boundary" }
}

# A real provider that never calls AWS: it renders the policy documents, so their size can be
# checked. Everything that would need an API call (the KMS keys, buckets) is overridden.
provider "aws" {
  alias                       = "offline"
  region                      = "us-west-2"
  access_key                  = "offline"
  secret_key                  = "offline"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
}

variables {
  region         = "us-west-2"
  aws_account_id = "123456789012"
  mandatory_tags = {
    created_by           = "test"
    created_date         = "2026-01-01"
    owner                = "test"
    internal_client_code = "test"
    client               = "test"
    project_name         = "test"
    division             = "test"
  }
  deploy_principal_arns = ["arn:aws:iam::123456789012:user/alice"]
}

run "deploy_principals_relative_to_the_account_get_its_arn" {
  command = plan

  variables {
    deploy_principal_arns = ["user/alice", "arn:aws:iam::210987654321:role/ops"]
  }

  assert {
    condition     = local.deploy_principals == ["arn:aws:iam::123456789012:user/alice", "arn:aws:iam::210987654321:role/ops"]
    error_message = "Relative deploy principals get this account's ARN (aws_account_id); full ARNs are kept."
  }
}

run "refuses_to_lock_the_applying_admin_out_of_the_state" {
  command = plan

  variables {
    deploy_principal_arns = ["user/bob"] # alice is applying
  }

  expect_failures = [data.aws_iam_policy_document.state_access]
}

run "an_assumed_role_session_counts_as_its_role" {
  command = plan

  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012", arn = "arn:aws:sts::123456789012:assumed-role/admins/alice" }
  }

  variables {
    deploy_principal_arns = ["role/admins"]
  }

  assert {
    condition     = local.caller_principal == "arn:aws:iam::123456789012:role/admins"
    error_message = "The guard compares the session's role ARN, as the bucket policy does."
  }
}

run "one_deploy_role_per_environment" {
  command = plan

  assert {
    condition = (
      aws_iam_role.deploy["dev"].name == "entingestion-dev-deploy"
      && aws_iam_role.deploy["prod"].name == "entingestion-prod-deploy"
      && aws_iam_role.deploy["dev"].max_session_duration == 3600
      && aws_iam_role.deploy["dev"].tags["env"] == "dev"
    )
    error_message = "Each environment gets its own one-hour deploy role, tagged with its env."
  }

  assert {
    condition = (
      aws_iam_role.deploy["dev"].permissions_boundary == "arn:aws:iam::123456789012:policy/entingestion-dev-deploy-boundary"
      && aws_iam_role.deploy["prod"].permissions_boundary == "arn:aws:iam::123456789012:policy/entingestion-prod-deploy-boundary"
    )
    error_message = "Every deploy role must carry its own environment's permissions boundary."
  }
}

run "boundaries_fit_the_managed_policy_size_limit" {
  command = plan

  providers = {
    aws = aws.offline
  }

  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012", arn = "arn:aws:iam::123456789012:user/alice" }
  }

  override_module {
    target  = module.kms
    outputs = { key_arn = "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000", key_id = "k", alias_arn = "a", alias_name = "alias/entingestion-tfstate" }
  }

  override_module {
    target  = module.trail_kms
    outputs = { key_arn = "arn:aws:kms:us-west-2:123456789012:key/11111111-1111-1111-1111-111111111111", key_id = "k", alias_arn = "a", alias_name = "alias/entingestion-trail" }
  }

  override_module {
    target  = module.trail_bucket
    outputs = { id = "entingestion-trail-123456789012-us-west-2", arn = "arn:aws:s3:::entingestion-trail-123456789012-us-west-2", regional_domain_name = "d", settings = {} }
  }

  override_module {
    target  = module.state_bucket
    outputs = { id = "entingestion-tfstate-123456789012-us-west-2", arn = "arn:aws:s3:::entingestion-tfstate-123456789012-us-west-2", regional_domain_name = "d", settings = {} }
  }

  # IAM counts a managed policy's characters without whitespace, at most 6,144.
  assert {
    condition = alltrue([
      for env in ["dev", "prod"] :
      length(replace(data.aws_iam_policy_document.deploy_boundary[env].json, "/\\s/", "")) < 6144
      && length(replace(data.aws_iam_policy_document.workload_boundary[env].json, "/\\s/", "")) < 6144
    ])
    error_message = "A boundary is over the 6,144-character managed policy limit; move statements to a deny policy on the role."
  }
}

run "people_need_mfa_and_github_is_off_by_default" {
  command = plan

  assert {
    condition     = length(aws_iam_openid_connect_provider.github) == 0
    error_message = "No GitHub OIDC provider without github_repository."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.deploy_trust["dev"].statement :
      s.sid == "People"
      && anytrue([for p in s.principals : p.identifiers == toset(["arn:aws:iam::123456789012:user/alice"])])
      && anytrue([for c in s.condition : c.variable == "aws:MultiFactorAuthPresent" && contains(c.values, "true")])
    ])
    error_message = "Only the listed people may assume the role, and only with MFA."
  }
}

run "mfa_condition_can_be_turned_off" {
  command = plan

  variables {
    require_mfa = false
  }

  assert {
    condition     = alltrue([for s in data.aws_iam_policy_document.deploy_trust["dev"].statement : length(s.condition) == 0])
    error_message = "require_mfa = false drops the MFA condition."
  }
}

run "github_trust_is_scoped_to_repo_and_environment" {
  command = plan

  variables {
    github_repository = "acme/entingestion"
  }

  assert {
    condition     = length(aws_iam_openid_connect_provider.github) == 1
    error_message = "github_repository creates the OIDC provider."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.deploy_trust["prod"].statement :
      s.sid == "GitHubActions" && anytrue([
        for c in s.condition : c.variable == "token.actions.githubusercontent.com:sub"
        && c.values == tolist(["repo:acme/entingestion:environment:prod"])
      ])
    ])
    error_message = "GitHub may assume the prod role only from the prod environment of that repo."
  }
}

run "boundary_denies_credentials_access_layer_state_and_other_envs" {
  command = plan

  assert {
    condition = alltrue([for sid in ["NoLongLivedCredentials", "NoChangesToThisAccessLayer", "KeepTheStateStore", "KeepTheStateKey", "NoAccountChanges", "OnlyOwnState", "NotOtherEnvironmentsResources", "NotCreatingOtherEnvironmentsResources"] :
      anytrue([for s in data.aws_iam_policy_document.deploy_boundary["dev"].statement : s.sid == sid && s.effect == "Deny"])
    ])
    error_message = "The boundary must deny every guarded group."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.deploy_boundary["dev"].statement :
      s.sid == "NoLongLivedCredentials" && contains(s.actions, "iam:CreateAccessKey") && contains(s.actions, "iam:CreateUser")
    ])
    error_message = "Deploy roles must not mint access keys or users."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.deploy_boundary["dev"].statement :
      s.sid == "OnlyOwnState"
      && s.resources == toset([
        "arn:aws:s3:::entingestion-tfstate-123456789012-us-west-2/prod/*",
        "arn:aws:s3:::entingestion-tfstate-123456789012-us-west-2/bootstrap/*",
      ])
    ])
    error_message = "The dev role must not read or write prod or bootstrap state (and nothing else is cut off)."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.deploy_boundary["prod"].statement :
      s.sid == "NotOtherEnvironmentsResources" && anytrue([for c in s.condition : c.variable == "aws:ResourceTag/env" && c.values == tolist(["dev"])])
    ])
    error_message = "The prod role must not touch resources tagged env=dev."
  }
}

run "single_environment_has_no_cross_environment_denies" {
  command = plan

  variables {
    environments = ["dev"]
  }

  assert {
    condition = !anytrue([
      for s in data.aws_iam_policy_document.deploy_boundary["dev"].statement :
      contains(["NotOtherEnvironmentsResources", "NotCreatingOtherEnvironmentsResources", "NotOtherEnvironmentsByName"], s.sid)
    ])
    error_message = "With one environment there is nothing to deny across."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.deploy_boundary["dev"].statement :
      s.sid == "OnlyOwnState" && s.resources == toset(["arn:aws:s3:::entingestion-tfstate-123456789012-us-west-2/bootstrap/*"])
    ])
    error_message = "The bootstrap state stays off limits even with one environment."
  }
}

run "state_bucket_admits_only_deploy_roles_people_and_root" {
  command = plan

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.state_access.statement :
      s.effect == "Deny" && anytrue([
        for c in s.condition : c.test == "StringNotLike" && c.variable == "aws:PrincipalArn"
        && toset(c.values) == toset([
          "arn:aws:iam::123456789012:role/entingestion-dev-deploy",
          "arn:aws:iam::123456789012:role/entingestion-prod-deploy",
          "arn:aws:iam::123456789012:user/alice",
          "arn:aws:iam::123456789012:root",
        ])
      ])
    ])
    error_message = "Everyone else in the account is denied the state bucket."
  }
}

run "deploy_roles_only_create_bounded_roles_of_their_environment" {
  command = plan

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.deploy_boundary["dev"].statement :
      s.sid == "RolesNeedTheWorkloadBoundary" && s.effect == "Deny"
      && s.actions == toset(["iam:CreateRole", "iam:PutRolePermissionsBoundary"])
      && anytrue([
        for c in s.condition : c.test == "StringNotEquals" && c.variable == "iam:PermissionsBoundary"
        && c.values == tolist(["arn:aws:iam::123456789012:policy/entingestion-dev-workload-boundary"])
      ])
    ])
    error_message = "The dev role may only create roles that carry the dev workload boundary."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.deploy_boundary["prod"].statement :
      s.sid == "KeepWorkloadBoundaries" && s.actions == toset(["iam:DeleteRolePermissionsBoundary"])
    ])
    error_message = "Nobody removes a workload boundary."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.deploy_boundary["dev"].statement :
      s.sid == "OnlyThisEnvironmentsRoles" && contains(s.actions, "iam:PassRole") && contains(s.actions, "iam:CreateRole")
      && s.not_resources == toset([
        "arn:aws:iam::123456789012:role/producer-dev-*",
        "arn:aws:iam::123456789012:role/ingestion-dev-*",
        "arn:aws:iam::123456789012:role/ui-dev-*",
      ])
    ])
    error_message = "The dev role may only manage and pass the dev stacks' roles."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.deploy_boundary["dev"].statement :
      s.sid == "OnlyThisEnvironmentsPolicies" && contains(s.actions, "iam:CreatePolicy")
      && s.not_resources == toset([
        "arn:aws:iam::123456789012:policy/ingestion-dev-*",
        "arn:aws:iam::123456789012:policy/producer-*-dev",
        "arn:aws:iam::123456789012:policy/producer-dev-*",
        "arn:aws:iam::123456789012:policy/ui-dev-*",
      ])
    ])
    error_message = "The dev role may only manage the dev stacks' policies (producer, ingestion, ui)."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.deploy_boundary["dev"].statement :
      s.sid == "NoPrivilegedManagedPolicies" && anytrue([
        for c in s.condition : c.variable == "iam:PolicyARN" && contains(c.values, "arn:aws:iam::aws:policy/AdministratorAccess")
        && contains(c.values, "arn:aws:iam::aws:policy/IAMFullAccess") && contains(c.values, "arn:aws:iam::aws:policy/PowerUserAccess")
      ])
    ])
    error_message = "No role may be given AdministratorAccess, IAMFullAccess or PowerUserAccess."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.deploy_boundary["dev"].statement :
      s.sid == "NoChangesToThisAccessLayer" && contains(s.resources, "arn:aws:iam::123456789012:policy/entingestion-*")
    ])
    error_message = "Both boundaries (every entingestion-* policy) are part of the protected access layer."
  }
}

run "deploy_roles_keep_away_from_other_environments_by_name" {
  command = plan

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.deploy_boundary["dev"].statement :
      s.sid == "NotOtherEnvironmentsByName" && alltrue([
        for r in [
          "arn:aws:lambda:*:*:function:*-prod-*",
          "arn:aws:s3:::*-prod-*",
          "arn:aws:ssm:*:*:parameter/entingestion/prod/*",
          "arn:aws:cloudfront::*:function/*-prod-*",
          "arn:aws:dynamodb:*:*:table/articles",
          "arn:aws:dynamodb:*:*:table/articles/*",
          "arn:aws:dynamodb:*:*:table/ingestion_status",
        ] : contains(s.resources, r)
      ])
      && !anytrue([for r in s.resources : strcontains(r, "-dev-") || strcontains(r, "table/dev-")])
    ])
    error_message = "The dev role is denied prod's functions (Lambda and CloudFront), buckets, parameters and (unprefixed) tables by name, and nothing of dev."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.deploy_boundary["prod"].statement :
      s.sid == "NotOtherEnvironmentsByName" && contains(s.resources, "arn:aws:dynamodb:*:*:table/dev-*")
      && !contains(s.resources, "arn:aws:dynamodb:*:*:table/articles")
    ])
    error_message = "The prod role is denied dev's prefixed tables, not its own."
  }
}

run "state_store_and_trail_are_kept" {
  command = plan

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.deploy_boundary["dev"].statement :
      s.sid == "KeepTheStateStore" && alltrue([
        for a in ["s3:PutBucket*", "s3:DeleteBucket*", "s3:PutReplicationConfiguration", "s3:PutLifecycleConfiguration", "s3:PutEncryptionConfiguration"] :
        contains(s.actions, a)
      ])
    ])
    error_message = "Deploy roles can't reconfigure, replicate or delete the state bucket."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.deploy_boundary["dev"].statement :
      s.sid == "KeepTheAuditTrail" && contains(s.actions, "cloudtrail:StopLogging") && contains(s.actions, "cloudtrail:DeleteTrail")
      && contains(s.resources, "arn:aws:cloudtrail:us-west-2:123456789012:trail/entingestion-account-trail")
    ])
    error_message = "Deploy roles can't stop or delete the audit trail."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.state_access.statement :
      s.sid == "NoReplicationExceptRoot" && s.effect == "Deny" && s.actions == toset(["s3:PutReplicationConfiguration"])
      && anytrue([for c in s.condition : c.test == "StringNotEquals" && c.values == tolist(["arn:aws:iam::123456789012:root"])])
    ])
    error_message = "Only the account root may replicate the state bucket."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.state_access.statement :
      s.sid == "OnlyKmsEncryptedState" && s.actions == toset(["s3:PutObject"])
      && anytrue([for c in s.condition : c.test == "StringNotEqualsIfExists" && c.variable == "s3:x-amz-server-side-encryption" && c.values == tolist(["aws:kms"])])
    ])
    error_message = "State objects may only be written SSE-KMS (or with the bucket default)."
  }
}

run "workload_boundary_denies_iam_state_and_other_environments" {
  command = plan

  assert {
    condition = (
      aws_iam_policy.workload_boundary["dev"].name == "entingestion-dev-workload-boundary"
      && aws_iam_policy.workload_boundary["prod"].name == "entingestion-prod-workload-boundary"
    )
    error_message = "One workload boundary per environment, named as the stacks compute it."
  }

  assert {
    condition = alltrue([for sid in ["NoIAM", "NoAccountChanges", "NoTrailChanges", "NotTheStateOrTheTrail", "NotOtherEnvironmentsResources", "NotOtherEnvironmentsByName"] :
      anytrue([for s in data.aws_iam_policy_document.workload_boundary["dev"].statement : s.sid == sid && s.effect == "Deny"])
    ])
    error_message = "The workload boundary must deny IAM, account, trail, state and other-environment access."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.workload_boundary["dev"].statement :
      s.sid == "NoIAM" && s.actions == toset(["iam:*"]) && s.resources == toset(["*"])
    ])
    error_message = "Workloads never manage IAM, so a bounded role can't create an unbounded one."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.workload_boundary["dev"].statement :
      s.sid == "NotTheStateOrTheTrail" && contains(s.resources, "arn:aws:s3:::entingestion-tfstate-123456789012-us-west-2/*")
      && contains(s.resources, "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000")
    ])
    error_message = "Workloads can't touch the state bucket or its key."
  }
}

run "account_trail_is_multi_region_validated_and_covers_state" {
  command = plan

  variables {
    trail_data_event_bucket_arns = ["arn:aws:s3:::producer-prod-raw-123456789012-us-west-2"]
  }

  assert {
    condition = (
      aws_cloudtrail.account.name == "entingestion-account-trail"
      && aws_cloudtrail.account.is_multi_region_trail
      && aws_cloudtrail.account.include_global_service_events
      && aws_cloudtrail.account.enable_log_file_validation
      && aws_cloudtrail.account.kms_key_id == module.trail_kms.key_arn
      && aws_cloudtrail.account.cloud_watch_logs_group_arn == null
    )
    error_message = "One multi-region, validated, KMS-encrypted trail without CloudWatch Logs."
  }

  assert {
    condition = anytrue([
      for sel in aws_cloudtrail.account.advanced_event_selector :
      anytrue([for f in sel.field_selector : f.field == "eventCategory" && f.equals == tolist(["Management"])])
      && !anytrue([for f in sel.field_selector : f.field == "readOnly"])
    ])
    error_message = "All management events, read and write."
  }

  assert {
    condition = anytrue([
      for sel in aws_cloudtrail.account.advanced_event_selector :
      anytrue([
        for f in sel.field_selector : f.field == "resources.ARN" && f.starts_with == tolist([
          "arn:aws:s3:::entingestion-tfstate-123456789012-us-west-2/",
          "arn:aws:s3:::producer-prod-raw-123456789012-us-west-2/",
        ])
      ])
    ])
    error_message = "Object events for the state bucket and the listed buckets."
  }

  assert {
    condition = (
      module.trail_bucket.settings.versioning_status == "Enabled"
      && anytrue([for r in module.trail_bucket.settings.lifecycle_rules : r.expiration_days == 365 && r.noncurrent_version_expiration_days == 30])
    )
    error_message = "Trail logs are versioned and expire after trail_retention_days (noncurrent after 30)."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.trail_bucket.statement :
      anytrue([for c in s.condition : c.variable == "aws:SourceArn" && c.values == tolist(["arn:aws:cloudtrail:us-west-2:123456789012:trail/entingestion-account-trail"])])
    ])
    error_message = "Only this trail may write to the trail bucket."
  }
}

run "rejects_bad_trail_inputs" {
  command = plan

  variables {
    trail_retention_days         = 7
    trail_data_event_bucket_arns = ["arn:aws:s3:::bucket/key"]
  }

  expect_failures = [var.trail_retention_days, var.trail_data_event_bucket_arns]
}

run "rejects_bad_inputs" {
  command = plan

  variables {
    deploy_principal_arns = []
    github_repository     = "not a repo"
  }

  expect_failures = [var.deploy_principal_arns, var.github_repository]
}
