# Janitor (hourly): settles tickets that were never used (or whose S3 event was lost) and closes
# processes left OPEN, so every process can settle (services/producer/src/producer/janitor.py).

data "aws_iam_policy_document" "janitor" {
  statement {
    sid       = "PendingUploads" # gsi_pending; EXPIRED / resent_at (UpdateItem, in transactions)
    actions   = ["dynamodb:Query", "dynamodb:UpdateItem"]
    resources = [module.uploads_table.arn, module.uploads_table.index_arns]
  }

  statement {
    sid       = "Processes" # idle OPEN processes (a scan of the small process table); counters
    actions   = ["dynamodb:Scan", "dynamodb:UpdateItem"]
    resources = [module.process_table.arn]
  }

  statement {
    sid       = "ObjectExists" # HeadObject needs s3:GetObject
    actions   = ["s3:GetObject"]
    resources = ["${module.raw_bucket.arn}/raw/*"]
  }

  statement {
    sid       = "ObjectExistsList" # 404 rather than 403 for a missing object
    actions   = ["s3:ListBucket"]
    resources = [module.raw_bucket.arn]
  }

  statement {
    sid       = "ResendToRegistrar"
    actions   = ["sqs:SendMessage"]
    resources = [module.registrar_queue.queue_arn]
  }

  statement {
    sid       = "DataKey"
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"]
    resources = [module.kms.key_arn]
  }
}

module "janitor_function" {
  source = "../../modules/lambda_function"

  name        = "${local.name_prefix}-janitor"
  description = "Producer janitor (${var.env}): expired tickets, idle processes"
  handler     = "producer.janitor.handler"
  memory_size = 256
  timeout     = 300
  code        = module.lambda_artifact.code
  environment = merge(local.function_environment, {
    REGISTRAR_QUEUE_URL     = module.registrar_queue.queue_url
    OPEN_PROCESS_IDLE_DAYS  = tostring(var.open_process_idle_days)
    POWERTOOLS_SERVICE_NAME = "producer-janitor"
  })
  policy_json          = data.aws_iam_policy_document.janitor.json
  kms_key_arn          = module.kms.key_arn
  log_retention_days   = var.log_retention_days
  permissions_boundary = local.permissions_boundary
}

module "janitor_schedule" {
  source = "../../modules/scheduler_schedule"

  name                  = "${local.name_prefix}-janitor"
  description           = "Producer janitor (${var.env})"
  schedule_expression   = var.janitor_schedule
  target_arn            = module.janitor_function.alias_arn
  kms_key_arn           = module.kms.key_arn
  retry_attempts        = 1
  max_event_age_seconds = 900
  permissions_boundary  = local.permissions_boundary
}
