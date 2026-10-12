# Lambda functions (brief §8.3). Two packages: core and docs (services/ingestion/scripts/build_lambda.sh).

# Both zips are over the 50 MB direct-upload limit, so Lambda reads them from S3.
module "artifacts" {
  source = "../../modules/s3_bucket"

  name          = "ingest-artifacts-${var.env}-${local.account_id}"
  kms_key_arn   = module.kms.key_arn
  versioning    = true
  force_destroy = !local.is_prod

  lifecycle_rules = [
    {
      id                                     = "old-builds"
      abort_incomplete_multipart_upload_days = 1
      noncurrent_version_expiration_days     = 30
    },
  ]
}

locals {
  package_zips = {
    core = var.lambda_core_zip
    docs = var.lambda_docs_zip
  }
}

resource "aws_s3_object" "package" {
  for_each = local.package_zips

  bucket      = module.artifacts.id
  key         = "lambda/${each.key}.zip"
  source      = each.value
  source_hash = filemd5(each.value)
  kms_key_id  = module.kms.key_arn
}

locals {
  packages = {
    for name, zip in local.package_zips : name => {
      s3_bucket        = aws_s3_object.package[name].bucket
      s3_key           = aws_s3_object.package[name].key
      source_code_hash = filebase64sha256(zip)
    }
  }

  common_env = {
    PIPELINE_VERSION             = var.pipeline_version
    STATUS_TABLE                 = module.status_table.name
    CACHE_TABLE                  = module.cache_table.name
    STAGING_BUCKET               = module.staging.id
    PRODUCER_ARTICLES_TABLE      = local.producer.articles_table_name
    PRODUCER_PROCESS_TABLE       = local.producer.process_table_name
    EVENT_BUS_NAME               = "default"
    POWERTOOLS_SERVICE_NAME      = "ingestion"
    POWERTOOLS_METRICS_NAMESPACE = "Ingestion"
    POWERTOOLS_LOG_LEVEL         = var.log_level
    OCR_ENABLED                  = tostring(var.ocr_enabled)
    LEASE_SECONDS                = tostring(var.lease_seconds)
    INGEST_QUEUE_URL             = module.ingest_queue.queue_url # a run hands a pending delete over
  }

  # name => settings. vpc: which security group, if the function runs in the VPC. Every
  # function gets the embedding profiles (embedding.tf) with common_env.
  functions = {
    dispatcher      = { handler = "ingest.dispatcher.handler", package = "core", memory = 256, timeout = local.dispatcher_timeout, reserved = var.dispatcher_reserved_concurrency, vpc = null, env = local.dispatcher_env }
    claim           = { handler = "ingest.steps.claim.handler", package = "core", memory = 256, timeout = 30, reserved = null, vpc = null, env = {} }
    fetch           = { handler = "ingest.steps.fetch.handler", package = "core", memory = 1024, timeout = 300, reserved = null, vpc = null, env = {} }
    extract         = { handler = "ingest.steps.extract.handler", package = "docs", memory = 2048, timeout = 900, reserved = null, vpc = null, env = {} }
    normalize       = { handler = "ingest.steps.normalize.handler", package = "docs", memory = 1024, timeout = 300, reserved = null, vpc = null, env = local.pii_env }
    chunk           = { handler = "ingest.steps.chunk.handler", package = "docs", memory = 1024, timeout = 300, reserved = null, vpc = null, env = { TIKTOKEN_CACHE_DIR = "/var/task/tiktoken_cache", PARENT_CHILD_ENABLED = tostring(var.parent_child_enabled), MAX_CHUNKS_PER_ARTICLE = tostring(var.max_chunks_per_article) } }
    embed           = { handler = "ingest.steps.embed.handler", package = "core", memory = 1024, timeout = 900, reserved = var.embed_reserved_concurrency, vpc = null, env = { EMBED_CONCURRENCY = tostring(var.embed_threads) } }
    validate        = { handler = "ingest.steps.validate.handler", package = "core", memory = 1024, timeout = 300, reserved = null, vpc = null, env = {} }
    index           = { handler = "ingest.steps.index.handler", package = "core", memory = 1024, timeout = 300, reserved = var.index_reserved_concurrency, vpc = "db_client", env = local.db_env }
    finalize        = { handler = "ingest.steps.finalize.handler", package = "core", memory = 256, timeout = 60, reserved = null, vpc = null, env = {} }
    mark_failed     = { handler = "ingest.steps.mark_failed.handler", package = "core", memory = 256, timeout = 60, reserved = null, vpc = null, env = {} }
    migrate         = { handler = "ingest.migrate.handler", package = "core", memory = 512, timeout = 300, reserved = null, vpc = "migrate", env = local.migrate_env }
    ocr_start       = { handler = "ingest.steps.ocr.start", package = "core", memory = 256, timeout = 60, reserved = null, vpc = null, env = {} }
    ocr_poll        = { handler = "ingest.steps.ocr.poll", package = "core", memory = 256, timeout = 30, reserved = null, vpc = null, env = { OCR_MAX_POLLS = tostring(ceil(var.ocr_max_wait_seconds / var.ocr_poll_seconds)) } }
    ocr_collect     = { handler = "ingest.steps.ocr.collect", package = "core", memory = 2048, timeout = 900, reserved = null, vpc = null, env = {} }
    control_api     = { handler = "ingest.control_api.app.handler", package = "core", memory = 512, timeout = 29, reserved = var.control_api_reserved_concurrency, vpc = null, env = merge(local.queue_env, local.reindex_env, local.search_env) }
    reindex_plan    = { handler = "ingest.reindex.plan", package = "core", memory = 512, timeout = 300, reserved = null, vpc = null, env = {} }
    reindex_enqueue = { handler = "ingest.reindex.enqueue", package = "core", memory = 512, timeout = 300, reserved = null, vpc = null, env = local.queue_env }
    query           = { handler = "ingest.query.handler", package = "core", memory = 512, timeout = 60, reserved = null, vpc = "db_client", env = merge(local.db_env, { DB_USER = local.db_reader }) }
    tables          = { handler = "ingest.tables.handler", package = "core", memory = 256, timeout = 60, reserved = null, vpc = "db_client", env = merge(local.db_env, { DB_USER = local.db_ops }) }
    eval            = { handler = "ingest.evaluation.handler", package = "core", memory = 1024, timeout = 300, reserved = null, vpc = null, env = { QUERY_FUNCTION_NAME = "${local.query_function_name}:${local.fn_alias}", EMBED_CONCURRENCY = tostring(var.embed_threads) } }
    reconciler      = { handler = "ingest.reconciler.app.handler", package = "core", memory = 512, timeout = 300, reserved = null, vpc = null, env = merge(local.queue_env, { ALERT_TOPIC_ARN = module.alerts.topic_arn, RECONCILE_BATCH = tostring(var.reconcile_batch), QUERY_FUNCTION_NAME = "${local.query_function_name}:${local.fn_alias}" }) }
  }

  queue_env = {
    INGEST_QUEUE_URL   = module.ingest_queue.queue_url
    INGEST_DLQ_URL     = module.ingest_queue.dlq_url
    BACKFILL_QUEUE_URL = module.backfill_queue.queue_url
  }

  # Admission control (ingest/dispatcher.py): slots per lane, and where unusable bodies go.
  live_slots = coalesce(var.live_slots, var.embed_reserved_concurrency == null ? 8 : 2 * var.embed_reserved_concurrency)
  dispatcher_env = {
    STATE_MACHINE_ARN  = local.state_machine_arn
    LIVE_SLOTS         = tostring(local.live_slots)
    BACKFILL_SLOTS     = tostring(var.backfill_slots)
    BACKFILL_QUEUE_ARN = module.backfill_queue.queue_arn
    INGEST_DLQ_URL     = module.ingest_queue.dlq_url
    BACKFILL_DLQ_URL   = module.backfill_queue.dlq_url
  }

  # Computed names, not references: the reindex machine already depends on module.fn. Callers
  # invoke the live aliases (name:alias), so a rollback of these functions takes effect too.
  reindex_env = {
    REINDEX_STATE_MACHINE_ARN = local.reindex_state_machine_arn
    TABLES_FUNCTION_NAME      = "${local.tables_function_name}:${local.fn_alias}"
  }

  # POST /v1/search: authorize against the producer's source policy, embed, query, rerank.
  search_env = {
    QUERY_FUNCTION_NAME     = "${local.query_function_name}:${local.fn_alias}"
    SOURCE_ACCESS_PARAMETER = local.source_access_parameter
    RERANK_ENABLED          = tostring(var.rerank_enabled)
    RERANK_MODEL_ARN        = local.rerank_model_arn
    # The switch gate (POST /v1/tables/active) reads the tables' newest evaluation reports.
    EVAL_TOLERANCE    = tostring(var.eval_tolerance)
    EVAL_MAX_AGE_DAYS = tostring(var.eval_max_age_days)
  }

  pii_env = {
    PII_SCREENING           = tostring(var.pii_screening)
    PII_MIN_SCORE           = tostring(var.pii_min_score)
    SOURCE_ACCESS_PARAMETER = local.source_access_parameter
  }

  db_env = {
    DB_HOST        = local.db_host
    DB_PORT        = "5432"
    DB_NAME        = local.db_name
    DB_USER        = local.db_writer
    DB_SSLMODE     = "verify-full"
    DB_SSLROOTCERT = local.db_sslrootcert
  }
  migrate_env = {
    DB_DIRECT_HOST = local.db_direct_host
    DB_SECRET_ARN  = local.db_master_secret
    DB_NAME        = local.db_name
    ALLOW_TRUNCATE = tostring(var.allow_truncate)
  }

  vpc_security_groups = {
    db_client = module.db_client_sg.id
    migrate   = module.migrate_sg.id
  }

  policies = {
    dispatcher      = data.aws_iam_policy_document.dispatcher.json
    claim           = data.aws_iam_policy_document.claim.json
    fetch           = data.aws_iam_policy_document.fetch.json
    extract         = data.aws_iam_policy_document.extract.json
    normalize       = data.aws_iam_policy_document.normalize.json
    chunk           = data.aws_iam_policy_document.chunk.json
    embed           = data.aws_iam_policy_document.embed.json
    validate        = data.aws_iam_policy_document.validate.json
    index           = data.aws_iam_policy_document.index.json
    finalize        = data.aws_iam_policy_document.finalize.json
    mark_failed     = data.aws_iam_policy_document.mark_failed.json
    migrate         = data.aws_iam_policy_document.migrate.json
    ocr_start       = data.aws_iam_policy_document.ocr_start.json
    ocr_poll        = data.aws_iam_policy_document.ocr_poll.json
    ocr_collect     = data.aws_iam_policy_document.ocr_collect.json
    control_api     = data.aws_iam_policy_document.control_api.json
    reindex_plan    = data.aws_iam_policy_document.reindex_plan.json
    reindex_enqueue = data.aws_iam_policy_document.reindex_enqueue.json
    query           = data.aws_iam_policy_document.query.json
    tables          = data.aws_iam_policy_document.tables.json
    eval            = data.aws_iam_policy_document.eval.json
    reconciler      = data.aws_iam_policy_document.reconciler.json
  }

  workflow_functions = ["claim", "fetch", "extract", "ocr_start", "ocr_poll", "ocr_collect", "normalize", "chunk", "embed", "validate", "index", "finalize", "mark_failed"]
}

module "fn" {
  source   = "../../modules/lambda_function"
  for_each = local.functions

  name                           = "${local.name_prefix}-${replace(each.key, "_", "-")}"
  description                    = "Ingestion ${each.key} (${var.env})"
  handler                        = each.value.handler
  memory_size                    = each.value.memory
  timeout                        = each.value.timeout
  code                           = local.packages[each.value.package]
  environment                    = merge(local.common_env, local.embedding_env, each.value.env)
  policy_json                    = local.policies[each.key]
  kms_key_arn                    = module.kms.key_arn
  log_retention_days             = var.log_retention_days
  reserved_concurrent_executions = coalesce(each.value.reserved, -1)
  permissions_boundary           = local.permissions_boundary

  vpc_config = each.value.vpc == null ? null : {
    subnet_ids         = module.vpc.private_subnet_ids
    security_group_ids = [local.vpc_security_groups[each.value.vpc]]
  }
  # In-VPC functions have no route to X-Ray (no NAT, no X-Ray endpoint).
  tracing_mode = each.value.vpc == null ? "Active" : "PassThrough"
}
