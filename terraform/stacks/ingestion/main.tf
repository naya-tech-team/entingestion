# Data stores: KMS key, staging bucket, status + cache tables, queues, alert topic.

module "kms" {
  source = "../../modules/kms_key"

  alias       = local.name_prefix
  description = "Ingestion (${var.env}): status/cache tables, staging, logs, database, workflow."

  policy_statements = [
    {
      sid        = "CloudWatchLogsEncryption"
      actions    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
      principals = [{ type = "Service", identifiers = ["logs.${local.region}.amazonaws.com"] }]
      conditions = [{
        test     = "ArnLike"
        variable = "kms:EncryptionContext:aws:logs:arn"
        values   = ["arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:*"]
      }]
    },
    {
      sid        = "CloudWatchAlarmsToEncryptedTopic"
      actions    = ["kms:Decrypt", "kms:GenerateDataKey*"]
      principals = [{ type = "Service", identifiers = ["cloudwatch.amazonaws.com"] }]
      conditions = [{ test = "StringEquals", variable = "aws:SourceAccount", values = [local.account_id] }]
    },
  ]
}

module "staging" {
  source = "../../modules/s3_bucket"

  name          = "ingest-staging-${var.env}-${local.account_id}"
  kms_key_arn   = module.kms.key_arn
  versioning    = true
  force_destroy = !local.is_prod

  lifecycle_rules = [
    {
      id                                     = "versions"
      abort_incomplete_multipart_upload_days = 7
      noncurrent_version_transitions         = [{ days = var.staging_noncurrent_days, storage_class = "GLACIER_IR" }]
      noncurrent_version_expiration_days     = var.staging_noncurrent_expiration_days
    },
    {
      # Temp keys from interrupted writes (brief §5: write to _tmp, then copy).
      id              = "tmp"
      prefix          = "_tmp/"
      expiration_days = 1
    },
    {
      id              = "reindex-plans"
      prefix          = "_reindex/"
      expiration_days = 7
    },
  ]
}

locals {
  status_gsis = [
    { name = "gsi1-status", hash_key = "gsi1pk", range_key = "gsi1sk" },
    { name = "gsi2-process", hash_key = "gsi2pk", range_key = "gsi2sk" },
    { name = "gsi3-lease", hash_key = "gsi3pk", range_key = "gsi3sk" },
    { name = "gsi4-stepfail", hash_key = "gsi4pk", range_key = "gsi4sk" },
  ]
}

# Single-table status store (brief §6): LATEST, RUN#, RUN#..#STEP#, PROCESS# items.
module "status_table" {
  source = "../../modules/dynamodb_table"

  name      = "${var.table_prefix}ingestion_status"
  hash_key  = "pk"
  range_key = "sk"
  attributes = [
    { name = "pk", type = "S" },
    { name = "sk", type = "S" },
    { name = "gsi1pk", type = "S" },
    { name = "gsi1sk", type = "S" },
    { name = "gsi2pk", type = "S" },
    { name = "gsi2sk", type = "S" },
    { name = "gsi3pk", type = "S" },
    { name = "gsi3sk", type = "N" },
    { name = "gsi4pk", type = "S" },
    { name = "gsi4sk", type = "S" },
  ]
  global_secondary_indexes = local.status_gsis
  ttl_attribute            = "expires_at_epoch"
  kms_key_arn              = module.kms.key_arn
  deletion_protection      = local.is_prod
}

module "cache_table" {
  source = "../../modules/dynamodb_table"

  name                = "${var.table_prefix}embedding_cache"
  hash_key            = "cache_key"
  attributes          = [{ name = "cache_key", type = "S" }]
  ttl_attribute       = "expires_at_epoch"
  kms_key_arn         = module.kms.key_arn
  deletion_protection = local.is_prod
}

locals {
  dispatcher_timeout = 60
}

module "ingest_queue" {
  source = "../../modules/sqs_queue"

  name                       = "${local.name_prefix}-ingest"
  visibility_timeout_seconds = 6 * local.dispatcher_timeout
  max_receive_count          = var.queue_max_receive_count # records waiting for a slot are received often
}

module "backfill_queue" {
  source = "../../modules/sqs_queue"

  name                       = "${local.name_prefix}-backfill"
  visibility_timeout_seconds = 6 * local.dispatcher_timeout
  max_receive_count          = var.queue_max_receive_count # records waiting for a slot are received often
}

# Stream records the Pipe couldn't deliver after its retries.
resource "aws_sqs_queue" "pipe_dlq" {
  name                      = "${local.name_prefix}-pipe-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

# TLS only, like the module's queues.
data "aws_iam_policy_document" "pipe_dlq" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.pipe_dlq.arn]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "pipe_dlq" {
  queue_url = aws_sqs_queue.pipe_dlq.id
  policy    = data.aws_iam_policy_document.pipe_dlq.json
}
