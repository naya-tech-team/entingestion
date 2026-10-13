# Producer articles stream -> Pipe (STAGED inserts/updates and REMOVEs) -> ingest queue -> dispatcher.

data "aws_iam_policy_document" "pipe_producer_key" {
  statement {
    sid       = "ProducerStreamKey" # the articles table and its stream use the producer's key
    actions   = ["kms:Decrypt"]
    resources = [local.producer.kms_key_arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["dynamodb.${local.region}.amazonaws.com"]
    }
  }
}

module "pipe" {
  source = "../../modules/eventbridge_pipe"

  name               = "${local.name_prefix}-articles"
  source_stream_arn  = local.producer.articles_stream_arn
  target_queue_arn   = module.ingest_queue.queue_arn
  dlq_arn            = aws_sqs_queue.pipe_dlq.arn
  starting_position  = "LATEST"
  max_retries        = 5
  log_retention_days = var.log_retention_days
  kms_key_arn        = module.kms.key_arn
  policy_documents   = [data.aws_iam_policy_document.pipe_producer_key.json]
  filter_patterns = [
    jsonencode({ eventName = ["INSERT", "MODIFY"], dynamodb = { NewImage = { status = { S = ["STAGED"] } } } }),
    jsonencode({ eventName = ["REMOVE"] }),
  ]
  permissions_boundary = local.permissions_boundary
}

# Both mappings invoke the dispatcher's live alias (a rollback moves it). Switching function_name
# from the function to the alias updates the mapping in place; it keeps its UUID and position.
module "ingest_trigger" {
  source = "../../modules/lambda_sqs_trigger"

  function_name           = module.fn["dispatcher"].alias_arn
  queue_arn               = module.ingest_queue.queue_arn
  batch_size              = 10
  batching_window_seconds = 1
  max_concurrency         = var.dispatcher_max_concurrency
}

module "backfill_trigger" {
  source = "../../modules/lambda_sqs_trigger"

  function_name   = module.fn["dispatcher"].alias_arn
  queue_arn       = module.backfill_queue.queue_arn
  batch_size      = 10
  max_concurrency = 2 # low priority: never starves the ingest queue
}
