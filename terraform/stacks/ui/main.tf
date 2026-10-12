# Upload UI (docs/UI.md): CloudFront in front of a static site bucket and the BFF function URL,
# behind HTTP basic auth checked by a CloudFront Function.
#
#   browser -> CloudFront -- viewer-request function: basic auth (hash in a KeyValueStore)
#                |-- /*     -> S3 site bucket (OAC)
#                `-- /api/* -> Lambda function URL (OAC, AWS_IAM) ui-<env>-bff
#                                `-- SigV4 -> producer API + control API

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

# What this stack needs from the producer and ingestion stacks, found through SSM so it doesn't
# depend on their Terraform state.
data "aws_ssm_parameter" "upstream" {
  for_each = {
    producer_api_url           = "producer/api_url"
    producer_api_execution_arn = "producer/api_execution_arn"
    producer_raw_bucket_name   = "producer/raw_bucket_name"
    producer_kms_key_arn       = "producer/kms_key_arn"
    control_api_url            = "ingestion/control_api_url"
    control_api_execution_arn  = "ingestion/control_api_execution_arn"
  }
  name = "${var.ssm_prefix}/${var.env}/${each.value}"
}

locals {
  name_prefix = "ui-${var.env}"
  account_id  = data.aws_caller_identity.current.account_id
  region      = data.aws_region.current.region
  partition   = data.aws_partition.current.partition

  upstream = { for k, p in data.aws_ssm_parameter.upstream : k => p.value }

  # Browsers PUT files straight to the raw bucket with presigned URLs on its regional endpoint.
  raw_bucket_origin = "https://${local.upstream.producer_raw_bucket_name}.s3.${local.region}.amazonaws.com"

  # BFF package. The local zip is hashed only in zip mode, so S3 mode never reads the disk.
  use_s3_artifact = var.lambda_s3_key != null
  zip_hash        = one([for p in compact([local.use_s3_artifact ? "" : var.lambda_zip_path]) : filebase64sha256(p)])
  lambda_code = {
    zip_path         = local.use_s3_artifact ? null : var.lambda_zip_path
    s3_bucket        = var.lambda_s3_bucket
    s3_key           = var.lambda_s3_key
    source_code_hash = local.use_s3_artifact ? var.lambda_s3_source_code_hash : local.zip_hash
  }

  # Every role the stack creates carries the workload boundary (terraform/bootstrap/access.tf);
  # the deploy role may only create roles that do.
  permissions_boundary = var.workload_boundary_enabled ? coalesce(var.permissions_boundary_arn, "arn:${local.partition}:iam::${local.account_id}:policy/entingestion-${var.env}-workload-boundary") : null
}

# ---------------------------------------------------------------------------
# Site bucket: the built SPA (services/ui/web/dist), read only by this distribution
# ---------------------------------------------------------------------------

module "site_bucket" {
  # SSE-S3 (kms_key_arn = null): the built SPA holds no data, and SSE-KMS would need a $1/month
  # key plus a key policy for CloudFront. Add a CKV_AWS_145 skip here if checkov starts flagging it.
  source = "../../modules/s3_bucket"

  name          = "${local.name_prefix}-site-${local.account_id}-${local.region}"
  kms_key_arn   = null # SSE-S3
  versioning    = true
  force_destroy = true # a build artefact: make ui-deploy puts it back

  lifecycle_rules = [{
    id                                     = "expire-old-builds"
    abort_incomplete_multipart_upload_days = 7
    noncurrent_version_expiration_days     = 30
  }]

  policy_documents = [data.aws_iam_policy_document.site_read_by_cloudfront.json]
}

data "aws_iam_policy_document" "site_read_by_cloudfront" {
  statement {
    sid       = "OnlyThisDistributionReads"
    actions   = ["s3:GetObject"]
    resources = ["${module.site_bucket.arn}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.this.arn]
    }
  }
}

# ---------------------------------------------------------------------------
# Published for the producer stack: the raw bucket's CORS allows this origin.
# ---------------------------------------------------------------------------

resource "aws_ssm_parameter" "origin" {
  #checkov:skip=CKV2_AWS_34:A public URL, not a secret; plain String keeps the producer stack free of KMS grants.
  name        = "${var.ssm_prefix}/${var.env}/ui/origin"
  description = "UI ${var.env}: the browser origin of the upload UI (managed by Terraform)"
  type        = "String"
  value       = "https://${aws_cloudfront_distribution.this.domain_name}"
}

# The boundary of every IAM role the stack creates (plan assertions). A new module that creates
# a role belongs here.
locals {
  role_permissions_boundaries = flatten([
    module.bff.role_permissions_boundaries,
  ])
}

# Plan-time warning if a role misses the boundary (the deploy role's CreateRole would fail).
check "roles_carry_the_workload_boundary" {
  assert {
    condition     = alltrue([for b in local.role_permissions_boundaries : coalesce(b, "none") == coalesce(local.permissions_boundary, "none")])
    error_message = "A role in this stack doesn't carry the workload boundary (local.permissions_boundary)."
  }
}
