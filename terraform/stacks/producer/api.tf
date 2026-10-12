# API: API Gateway (REST, AWS_IAM) -> api Lambda (brief §4, §7.3).

locals {
  # Principals in tfvars are relative to this account ("role/<name>", "user/<name>", patterns
  # allowed); the full ARN is derived from the account id. A full ARN passes through unchanged.
  # operator_principals (from the environment) write every source.
  iam_arn_prefix = "arn:${local.partition}:iam::${var.aws_account_id}:"
  source_access = {
    for name, cfg in var.source_access : name => merge(cfg, {
      writers = [for p in distinct(concat(cfg.writers, var.operator_principals)) : startswith(p, "arn:") ? p : "${local.iam_arn_prefix}${p}"]
      readers = [for p in cfg.readers : startswith(p, "arn:") ? p : "${local.iam_arn_prefix}${p}"]
    })
  }
  admin_principals = [for p in var.admin_principals : startswith(p, "arn:") ? p : "${local.iam_arn_prefix}${p}"]

  # Per-source writers/readers and tenants (producer.authz); also published for ingestion.
  source_access_json = jsonencode({ sources = local.source_access, admins = local.admin_principals })

  # Shared by the api and registrar functions.
  function_environment = merge(
    {
      IDEMPOTENCY_TABLE            = module.idempotency_table.name
      PROCESS_TABLE                = module.process_table.name
      ARTICLES_TABLE               = module.articles_table.name
      UPLOADS_TABLE                = module.uploads_table.name
      RAW_BUCKET                   = module.raw_bucket.id
      RAW_PREFIX                   = "raw/"
      POWERTOOLS_SERVICE_NAME      = "producer"
      POWERTOOLS_METRICS_NAMESPACE = "Producer"
      POWERTOOLS_LOG_LEVEL         = var.log_level
      MAX_FILE_BYTES               = tostring(var.max_file_bytes)
      UPLOAD_URL_TTL_SECONDS       = tostring(var.upload_url_ttl_seconds)
      SOURCE_ACCESS                = local.source_access_json
    },
    length(var.allowed_content_types) == 0 ? {} : {
      ALLOWED_CONTENT_TYPES = join(",", var.allowed_content_types)
    },
  )
}

data "aws_iam_policy_document" "api" {
  # Least privilege per table, matching what producer.api actually calls.
  statement {
    sid       = "ProcessTable"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
    resources = [module.process_table.arn, module.process_table.index_arns]
  }

  statement {
    sid       = "IdempotencyRecords"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
    resources = [module.idempotency_table.arn]
  }

  statement {
    sid = "UploadsTable"
    # Tickets (PutItem), ticket sequences (UpdateItem), delete tombstones, listing (gsi_member,
    # gsi_process_uploads).
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
    resources = [module.uploads_table.arn, module.uploads_table.index_arns]
  }

  statement {
    sid = "ArticlesTable"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:BatchGetItem",
      "dynamodb:Query",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
    ]
    resources = [module.articles_table.arn, module.articles_table.index_arns]
  }

  # Presigned PUT URLs are signed with this role's credentials, so it needs PutObject itself.
  statement {
    sid       = "PresignRawUploads"
    actions   = ["s3:PutObject"]
    resources = ["${module.raw_bucket.arn}/raw/*"]
  }

  # DELETE ?purge=true erases every stored version of an article's raw files.
  statement {
    sid       = "PurgeRawObjects"
    actions   = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
    resources = ["${module.raw_bucket.arn}/raw/*"]
  }

  statement {
    sid       = "ListRawVersions"
    actions   = ["s3:ListBucketVersions"]
    resources = [module.raw_bucket.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["raw/*"]
    }
  }

  statement {
    sid       = "DataKey"
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"]
    resources = [module.kms.key_arn]
  }
}

module "api_function" {
  source = "../../modules/lambda_function"

  name                 = "${local.name_prefix}-api"
  description          = "Producer API (${var.env})"
  handler              = "producer.api.lambda_handler"
  memory_size          = 512
  timeout              = 29
  code                 = local.lambda_code
  environment          = local.function_environment
  policy_json          = data.aws_iam_policy_document.api.json
  kms_key_arn          = module.kms.key_arn
  log_retention_days   = var.log_retention_days
  permissions_boundary = local.permissions_boundary

  reserved_concurrent_executions = coalesce(var.api_reserved_concurrency, -1)
}

module "api" {
  source = "../../modules/rest_api_lambda_proxy"

  name                  = local.name_prefix
  description           = "Producer API (${var.env})"
  lambda_function_name  = module.api_function.function_name
  lambda_invoke_arn     = module.api_function.alias_invoke_arn
  lambda_qualifier      = module.api_function.alias_name
  stage_name            = "prod"
  throttle_rate_limit   = var.api_throttle_rate
  throttle_burst_limit  = var.api_throttle_burst
  log_retention_days    = var.log_retention_days
  log_kms_key_arn       = module.kms.key_arn
  manage_apigw_account  = var.manage_apigw_account
  invoke_policy_name    = "producer-api-invoke-${var.env}"
  invoke_resource_paths = ["*/v1/*"]
  permissions_boundary  = local.permissions_boundary
}

# The boundary of every IAM role the stack creates (plan assertions). A new module that creates
# a role belongs here.
locals {
  role_permissions_boundaries = flatten([
    module.api_function.role_permissions_boundaries,
    module.registrar_function.role_permissions_boundaries,
    module.api.role_permissions_boundaries,
    module.janitor_function.role_permissions_boundaries,
    module.janitor_schedule.role_permissions_boundaries,
    module.backup[*].role_permissions_boundaries,
  ])
}

# Plan-time warning if a role misses the boundary (the deploy role's CreateRole would fail).
check "roles_carry_the_workload_boundary" {
  assert {
    condition     = alltrue([for b in local.role_permissions_boundaries : coalesce(b, "none") == coalesce(local.permissions_boundary, "none")])
    error_message = "A role in this stack doesn't carry the workload boundary (local.permissions_boundary)."
  }
}

# Read-only callers (search services, dashboards): GET routes only. The module's invoke policy
# is the writer policy (every method). Which sources a caller may touch is decided in the API.
data "aws_iam_policy_document" "invoke_reader" {
  statement {
    sid       = "InvokeReadOnly"
    actions   = ["execute-api:Invoke"]
    resources = ["${module.api.execution_arn}/${module.api.stage_name}/GET/v1/*"]
  }
}

resource "aws_iam_policy" "invoke_reader" {
  name        = "producer-api-reader-${var.env}"
  description = "Read-only access (GET) to the producer API, ${var.env}."
  policy      = data.aws_iam_policy_document.invoke_reader.json
}

