data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  is_prod     = module.common.is_prod
  name_prefix = module.common.name_prefix
  account_id  = data.aws_caller_identity.current.account_id
  region      = data.aws_region.current.region
  partition   = data.aws_partition.current.partition

  raw_bucket_name = coalesce(var.raw_bucket_name, "${local.name_prefix}-raw-${local.account_id}-${local.region}")

  # Every role the stack creates carries the workload boundary (terraform/bootstrap/access.tf);
  # the deploy role may only create roles that do.
  permissions_boundary = module.common.permissions_boundary
}

# Lambda package: the local zip (default) or an artifact in S3 (CI).
module "lambda_artifact" {
  source = "../../modules/lambda_artifact"

  zip_path            = var.lambda_zip_path
  s3_bucket           = var.lambda_s3_bucket
  s3_key              = var.lambda_s3_key
  s3_source_code_hash = var.lambda_s3_source_code_hash
}

# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------

module "kms" {
  source = "../../modules/kms_key"

  alias       = local.name_prefix
  description = "Producer service data key (${var.env}): tables, raw bucket, log groups and alarm topic."

  allow_cloudwatch_logs         = true # the service's log groups
  allow_cloudwatch_alarms_topic = true # the alarm topic
}

# ---------------------------------------------------------------------------
# Raw upload bucket: raw/{article_id}/{upload_id}/{filename}, never overwritten
# ---------------------------------------------------------------------------

module "raw_bucket" {
  source = "../../modules/s3_bucket"

  name             = local.raw_bucket_name
  kms_key_arn      = module.kms.key_arn
  versioning       = true
  force_destroy    = !local.is_prod
  policy_documents = [data.aws_iam_policy_document.raw_uploads_only_by_ticket.json]

  lifecycle_rules = concat(
    [{
      id                                     = "abort-mpu-and-archive-noncurrent"
      abort_incomplete_multipart_upload_days = 7
      noncurrent_version_transitions         = [{ days = 30, storage_class = "GLACIER_IR" }]
      noncurrent_version_expiration_days     = var.raw_noncurrent_expiration_days
    }],
    # Objects no article uses (the registrar tags rejected and superseded uploads).
    [for state in ["rejected", "superseded"] : {
      id              = "expire-${state}-uploads"
      prefix          = "raw/"
      tags            = { "producer-unused" = state }
      expiration_days = var.unused_upload_expiration_days
    }],
  )

  # Browsers PUT straight to S3 with the headers returned in the upload ticket.
  cors_rules = length(local.upload_origins) == 0 ? [] : [{
    allowed_methods = ["PUT"]
    allowed_origins = local.upload_origins
    allowed_headers = ["content-type", "x-amz-checksum-sha256", "x-amz-meta-upload-id"]
    expose_headers  = ["ETag", "x-amz-version-id", "x-amz-checksum-sha256"]
    max_age_seconds = 3000
  }]
}

# The upload UI (terraform/stacks/ui) publishes its origin under <ssm_prefix>/<env>/ui/; nothing
# is there before the UI stack exists, so its first apply is followed by one of this stack.
data "aws_ssm_parameters_by_path" "ui" {
  path = "${var.ssm_prefix}/${var.env}/ui"
}

locals {
  # Only .../ui/origin is an origin (the stack may publish other values there). Origins aren't
  # secret; the data source marks every value sensitive.
  ui_parameters = zipmap(data.aws_ssm_parameters_by_path.ui.names, nonsensitive(data.aws_ssm_parameters_by_path.ui.values))
  upload_origins = distinct(concat(
    [for name, value in local.ui_parameters : value if endswith(name, "/origin")],
    var.ui_extra_origins,
  ))
}

# Only upload tickets put objects under raw/: presigned URLs are signed by the API function's
# role, so any other writer (even an admin) is refused, and no other KMS key may be named.
data "aws_iam_policy_document" "raw_uploads_only_by_ticket" {
  statement {
    sid       = "OnlyUploadTicketsWriteRaw"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${module.raw_bucket.arn}/raw/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringNotEquals"
      variable = "aws:PrincipalArn"
      values   = [module.api_function.role_arn]
    }
  }

  statement {
    sid       = "OnlyTheServiceKey"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${module.raw_bucket.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringNotEqualsIfExists"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [module.kms.key_arn]
    }
  }
}

# ---------------------------------------------------------------------------
# Tables. Names and keys are a contract with the ingestion pipeline.
# ---------------------------------------------------------------------------

module "process_table" {
  source = "../../modules/dynamodb_table"

  name     = "${var.table_prefix}process"
  hash_key = "process_id"
  attributes = [
    { name = "process_id", type = "S" },
    { name = "source", type = "S" },
    { name = "created_at", type = "S" },
  ]
  global_secondary_indexes = [{
    # GET /v1/processes?source=: a source's processes, newest first.
    name      = "gsi_source"
    hash_key  = "source"
    range_key = "created_at"
  }]
  kms_key_arn         = module.kms.key_arn
  deletion_protection = local.is_prod
}

# Idempotency-Key records for the API's POSTs (Powertools idempotency), kept for a day.
module "idempotency_table" {
  source = "../../modules/dynamodb_table"

  name                = "${var.table_prefix}producer_idempotency"
  hash_key            = "id"
  attributes          = [{ name = "id", type = "S" }]
  ttl_attribute       = "expiration"
  kms_key_arn         = module.kms.key_arn
  deletion_protection = false # a day's worth of disposable retry records
}

module "articles_table" {
  source = "../../modules/dynamodb_table"

  name     = "${var.table_prefix}articles"
  hash_key = "article_id"
  attributes = [
    { name = "article_id", type = "S" },
    { name = "updated_at", type = "S" },
    { name = "updated_shard", type = "S" },
  ]
  global_secondary_indexes = [
    {
      # Used by the ingestion reconciler: today's and yesterday's changes with their checksums,
      # a day spread over 16 partitions (<date>#00..15) so bulk loads don't throttle one.
      name               = "gsi_updated_shard"
      hash_key           = "updated_shard"
      range_key          = "updated_at"
      projection_type    = "INCLUDE"
      non_key_attributes = ["content_sha256", "process_id", "upload_id"]
    },
  ]
  stream_view_type    = "NEW_AND_OLD_IMAGES"
  kms_key_arn         = module.kms.key_arn
  deletion_protection = local.is_prod
}

module "uploads_table" {
  source = "../../modules/dynamodb_table"

  name     = "${var.table_prefix}producer_uploads"
  hash_key = "upload_id"
  attributes = [
    { name = "upload_id", type = "S" },
    { name = "member_process_id", type = "S" },
    { name = "member_article_id", type = "S" },
    { name = "pending", type = "S" },
    { name = "ticket_expires_epoch", type = "N" },
    { name = "process_id", type = "S" },
    { name = "created_at", type = "S" },
  ]
  global_secondary_indexes = [
    {
      # A process's articles, from its membership markers: stable, in article order.
      name            = "gsi_member"
      hash_key        = "member_process_id"
      range_key       = "member_article_id"
      projection_type = "KEYS_ONLY"
    },
    {
      # Uploads without an outcome, by ticket expiry: the janitor settles the expired ones.
      name               = "gsi_pending"
      hash_key           = "pending"
      range_key          = "ticket_expires_epoch"
      projection_type    = "INCLUDE"
      non_key_attributes = ["status", "s3_key", "process_id"]
    },
    {
      # GET /v1/processes/{id}/uploads: a process's uploads, oldest first. Membership markers
      # carry process_id and created_at too; the API filters them out by `kind`.
      name               = "gsi_process_uploads"
      hash_key           = "process_id"
      range_key          = "created_at"
      projection_type    = "INCLUDE"
      non_key_attributes = ["article_id", "filename", "content_type", "size_bytes", "status", "reason", "title", "external_id", "finished_at", "kind"]
    },
  ]
  ttl_attribute       = "expires_at_epoch"
  kms_key_arn         = module.kms.key_arn
  deletion_protection = local.is_prod
}
