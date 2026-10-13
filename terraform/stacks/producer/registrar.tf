# Registrar: S3 ObjectCreated (raw/) -> SQS (+DLQ) -> registrar Lambda.

locals {
  registrar_timeout = 300
}

# Only this bucket, in this account, may send to the queue.
data "aws_iam_policy_document" "registrar_queue" {
  statement {
    sid       = "AllowRawBucketNotifications"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:${local.partition}:sqs:${local.region}:${local.account_id}:${local.name_prefix}-registrar"]

    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [module.raw_bucket.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

module "registrar_queue" {
  source = "../../modules/sqs_queue"

  name                       = "${local.name_prefix}-registrar"
  visibility_timeout_seconds = 6 * local.registrar_timeout # 1800 s
  max_receive_count          = 5
  policy_documents           = [data.aws_iam_policy_document.registrar_queue.json]
}

resource "aws_s3_bucket_notification" "raw" {
  bucket = module.raw_bucket.id

  queue {
    id            = "registrar"
    queue_arn     = module.registrar_queue.queue_arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "raw/"
  }

  # S3 validates that it may send to the queue when the notification is created.
  depends_on = [module.registrar_queue]
}

data "aws_iam_policy_document" "registrar" {
  statement {
    sid = "UploadsTable"
    # Get + claim/finish (UpdateItem) + membership markers (PutItem) + the delete tombstone
    # (ConditionCheckItem), in transactions.
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:PutItem", "dynamodb:ConditionCheckItem"]
    resources = [module.uploads_table.arn]
  }

  statement {
    sid       = "ProcessCounters"
    actions   = ["dynamodb:UpdateItem"]
    resources = [module.process_table.arn]
  }

  statement {
    sid       = "ArticlesUpsert"
    actions   = ["dynamodb:UpdateItem"]
    resources = [module.articles_table.arn]
  }

  statement {
    sid       = "ReadRawObjects"
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:GetObjectAttributes"]
    resources = ["${module.raw_bucket.arn}/raw/*"]
  }

  # Rejected and superseded objects are tagged so the bucket's lifecycle expires them.
  statement {
    sid       = "TagUnusedObjects"
    actions   = ["s3:PutObjectTagging", "s3:PutObjectVersionTagging"]
    resources = ["${module.raw_bucket.arn}/raw/*"]
  }

  statement {
    sid       = "DataKey"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [module.kms.key_arn]
  }

  statement {
    sid = "ConsumeQueue"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [module.registrar_queue.queue_arn]
  }
}

module "registrar_function" {
  source = "../../modules/lambda_function"

  name                 = "${local.name_prefix}-registrar"
  description          = "Producer registrar (${var.env}): verifies uploads and writes articles"
  handler              = "producer.registrar.lambda_handler"
  memory_size          = 1024
  timeout              = local.registrar_timeout
  code                 = module.lambda_artifact.code
  environment          = local.function_environment
  policy_json          = data.aws_iam_policy_document.registrar.json
  kms_key_arn          = module.kms.key_arn
  log_retention_days   = var.log_retention_days
  permissions_boundary = local.permissions_boundary

  reserved_concurrent_executions = coalesce(var.registrar_reserved_concurrency, -1)
}

# Invokes the live alias (a rollback moves it); the change from the function is an in-place
# update of the mapping.
module "registrar_trigger" {
  source = "../../modules/lambda_sqs_trigger"

  queue_arn               = module.registrar_queue.queue_arn
  function_name           = module.registrar_function.alias_arn
  batch_size              = 10
  batching_window_seconds = 5
}

# Shared module since ingestion Phase 4; the existing mapping is kept, not replaced.
moved {
  from = aws_lambda_event_source_mapping.registrar
  to   = module.registrar_trigger.aws_lambda_event_source_mapping.this
}
