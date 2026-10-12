# Account audit trail (docs/AWS_ACCESS.md): who did what, in every region, kept in S3.
#
# Management events (read and write) from all regions, plus object-level events on the state
# bucket (who read or wrote which state file) and any trail_data_event_buckets. No CloudWatch
# Logs delivery: the first copy of management events is free and S3 storage is pennies, so the
# trail costs about nothing. Query it in the console's Event history (90 days) or with Athena.
# Log file validation lets `aws cloudtrail validate-logs` prove nothing was changed or removed.

locals {
  trail_name = "entingestion-account-trail"
  # Computed, not referenced: the key and bucket policies need it before the trail exists.
  trail_arn = "arn:${local.partition}:cloudtrail:${var.region}:${var.aws_account_id}:trail/${local.trail_name}"
}

module "trail_kms" {
  source = "../modules/kms_key"

  alias       = "entingestion-trail"
  description = "Encrypts the entingestion account CloudTrail logs."

  # AWS's CloudTrail key policy, limited to this trail. Reading the logs stays with IAM (the
  # account-root statement the module adds).
  policy_statements = [
    {
      sid        = "CloudTrailEncryptLogs"
      actions    = ["kms:GenerateDataKey*"]
      principals = [{ type = "Service", identifiers = ["cloudtrail.amazonaws.com"] }]
      # aws:SourceArn only: the bucket uses an S3 Bucket Key, which changes the encryption context.
      conditions = [{ test = "StringEquals", variable = "aws:SourceArn", values = [local.trail_arn] }]
    },
    {
      sid        = "CloudTrailDescribeKey"
      actions    = ["kms:DescribeKey"]
      principals = [{ type = "Service", identifiers = ["cloudtrail.amazonaws.com"] }]
      conditions = [{ test = "StringEquals", variable = "aws:SourceArn", values = [local.trail_arn] }]
    },
  ]
}

# The bucket owns every object (BucketOwnerEnforced), so ACLs play no part. CloudTrail still
# sends bucket-owner-full-control, the one ACL such a bucket accepts; AWS's bucket policy keeps
# that condition, and so does this one.
data "aws_iam_policy_document" "trail_bucket" {
  statement {
    sid       = "CloudTrailAclCheck"
    actions   = ["s3:GetBucketAcl"]
    resources = ["arn:${local.partition}:s3:::${local.trail_bucket_name}"]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
  }

  statement {
    sid       = "CloudTrailWrite"
    actions   = ["s3:PutObject"]
    resources = ["arn:${local.partition}:s3:::${local.trail_bucket_name}/AWSLogs/${var.aws_account_id}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
  }
}

locals {
  trail_bucket_name = "entingestion-trail-${var.aws_account_id}-${var.region}"
}

module "trail_bucket" {
  source = "../modules/s3_bucket"

  name             = local.trail_bucket_name
  kms_key_arn      = module.trail_kms.key_arn
  versioning       = true
  policy_documents = [data.aws_iam_policy_document.trail_bucket.json]

  lifecycle_rules = [{
    id                                     = "expire-trail-logs"
    abort_incomplete_multipart_upload_days = 7
    expiration_days                        = var.trail_retention_days
    noncurrent_version_expiration_days     = 30
  }]
}

resource "aws_cloudtrail" "account" {
  #checkov:skip=CKV2_AWS_10:No CloudWatch Logs delivery, to keep the trail free; query S3 with Athena or use Event history.
  #checkov:skip=CKV_AWS_252:Nobody consumes per-file delivery notifications; no SNS topic needed.
  name                          = local.trail_name
  s3_bucket_name                = module.trail_bucket.id
  kms_key_id                    = module.trail_kms.key_arn
  is_multi_region_trail         = true
  include_global_service_events = true
  enable_log_file_validation    = true

  # Management events: read and write (no readOnly selector means both).
  advanced_event_selector {
    name = "Management events"

    field_selector {
      field  = "eventCategory"
      equals = ["Management"]
    }
  }

  # Object-level events on the state bucket, and on any listed buckets.
  advanced_event_selector {
    name = "S3 object events: state and listed buckets"

    field_selector {
      field  = "eventCategory"
      equals = ["Data"]
    }

    field_selector {
      field  = "resources.type"
      equals = ["AWS::S3::Object"]
    }

    field_selector {
      field       = "resources.ARN"
      starts_with = [for arn in concat([local.state_bucket_arn], var.trail_data_event_bucket_arns) : "${arn}/"]
    }
  }

  # CloudTrail checks that it may write to the bucket and use the key when the trail is created.
  depends_on = [module.trail_bucket, module.trail_kms]
}
