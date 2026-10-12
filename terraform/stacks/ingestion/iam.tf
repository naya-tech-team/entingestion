# One least-privilege policy per function (brief §8.3). Producer resources are read-only.

locals {
  state_machine_name = "${local.name_prefix}-ingest"
  # Computed, not referenced, so the dispatcher doesn't depend on the state machine (which
  # depends on the step functions).
  state_machine_arn          = "arn:${local.partition}:states:${local.region}:${local.account_id}:stateMachine:${local.state_machine_name}"
  reindex_state_machine_name = "${local.name_prefix}-reindex"
  reindex_state_machine_arn  = "arn:${local.partition}:states:${local.region}:${local.account_id}:stateMachine:${local.reindex_state_machine_name}"
  tables_function_name       = "${local.name_prefix}-tables"
  tables_function_arn        = "arn:${local.partition}:lambda:${local.region}:${local.account_id}:function:${local.tables_function_name}"
  query_function_name        = "${local.name_prefix}-query"
  query_function_arn         = "arn:${local.partition}:lambda:${local.region}:${local.account_id}:function:${local.query_function_name}"
  # The alias callers invoke (modules/lambda_function). Grants name it and the unqualified
  # function: code from before the alias still calls the function by name until it's replaced.
  fn_alias           = "live"
  tables_invoke_arns = [local.tables_function_arn, "${local.tables_function_arn}:${local.fn_alias}"]
  query_invoke_arns  = [local.query_function_arn, "${local.query_function_arn}:${local.fn_alias}"]
  event_bus_arn      = "arn:${local.partition}:events:${local.region}:${local.account_id}:event-bus/default"
  rerank_model_arn   = "arn:${local.partition}:bedrock:${local.region}::foundation-model/${var.rerank_model_id}"
  # The producer's per-source access policy (search authorization, PII policy), published to SSM.
  source_access_parameter     = "${var.producer_ssm_prefix}/source_access"
  source_access_parameter_arn = "arn:${local.partition}:ssm:${local.region}:${local.account_id}:parameter${local.source_access_parameter}"
}

# -- shared statements -------------------------------------------------------------

data "aws_iam_policy_document" "status_rw" {
  statement {
    sid = "StatusTable"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:ConditionCheckItem",
    ]
    resources = [module.status_table.arn, module.status_table.index_arns]
  }

  statement {
    sid       = "IngestionKey"
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"]
    resources = [module.kms.key_arn]
  }

  # A run that meets a pending delete releases the article and queues the REMOVE.
  statement {
    sid       = "HandOverDelete"
    actions   = ["sqs:SendMessage"]
    resources = [module.ingest_queue.queue_arn]
  }
}

data "aws_iam_policy_document" "staging_rw" {
  statement {
    sid       = "StagingObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${module.staging.arn}/*"]
  }

  statement {
    sid       = "StagingList" # HeadObject on a missing key returns 404 (not 403) with this
    actions   = ["s3:ListBucket"]
    resources = [module.staging.arn]
  }
}

data "aws_iam_policy_document" "producer_raw_read" {
  statement {
    sid       = "ProducerRawObjects"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${local.producer.raw_bucket_arn}/raw/*"]
  }

  # The producer's key only as S3 uses it to decrypt those objects.
  statement {
    sid       = "ProducerKeyViaS3"
    actions   = ["kms:Decrypt"]
    resources = [local.producer.kms_key_arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${local.region}.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "producer_rows_read" {
  statement {
    sid       = "ProducerRows" # BatchGetItem: tenants of articles from before LATEST had them
    actions   = ["dynamodb:GetItem", "dynamodb:BatchGetItem"]
    resources = [local.producer.articles_table_arn, local.producer.process_table_arn]
  }

  statement {
    sid       = "ProducerKeyViaDynamoDB"
    actions   = ["kms:Decrypt"]
    resources = [local.producer.kms_key_arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["dynamodb.${local.region}.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "events" {
  statement {
    sid       = "IngestionEvents" # article.indexed, process.ingested
    actions   = ["events:PutEvents"]
    resources = [local.event_bus_arn]
  }
}

# -- per function -------------------------------------------------------------------

data "aws_iam_policy_document" "dispatcher" {
  statement {
    sid       = "StartIngest"
    actions   = ["states:StartExecution"]
    resources = [local.state_machine_arn]
  }

  statement {
    sid       = "StackKey" # execution input, and the status table (admission slots)
    actions   = ["kms:GenerateDataKey", "kms:Decrypt", "kms:Encrypt"]
    resources = [module.kms.key_arn]
  }

  statement {
    sid       = "ConsumeQueues"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
    resources = [module.ingest_queue.queue_arn, module.backfill_queue.queue_arn]
  }

  # Admission control: the #SLOTS items of ingestion_status, nothing else in it.
  statement {
    sid       = "AdmissionSlots"
    actions   = ["dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [module.status_table.arn]

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:LeadingKeys"
      values   = ["#SLOTS"]
    }
  }

  statement {
    sid       = "DeadLetterUnusable" # bodies that can never start skip the receive count
    actions   = ["sqs:SendMessage"]
    resources = [module.ingest_queue.dlq_arn, module.backfill_queue.dlq_arn]
  }
}

data "aws_iam_policy_document" "claim" {
  source_policy_documents = [
    data.aws_iam_policy_document.status_rw.json,
    data.aws_iam_policy_document.producer_rows_read.json,
    data.aws_iam_policy_document.events.json,
  ]
}

data "aws_iam_policy_document" "fetch" {
  source_policy_documents = [
    data.aws_iam_policy_document.status_rw.json,
    data.aws_iam_policy_document.producer_raw_read.json,
  ]
}

data "aws_iam_policy_document" "extract" {
  source_policy_documents = [
    data.aws_iam_policy_document.status_rw.json,
    data.aws_iam_policy_document.staging_rw.json,
    data.aws_iam_policy_document.producer_raw_read.json,
  ]
}

data "aws_iam_policy_document" "normalize" {
  source_policy_documents = [
    data.aws_iam_policy_document.status_rw.json,
    data.aws_iam_policy_document.staging_rw.json,
    data.aws_iam_policy_document.producer_rows_read.json,
  ]

  # PII screening (pii_screening): Comprehend, and each source's policy from the producer.
  dynamic "statement" {
    for_each = var.pii_screening ? ["DetectPii"] : []
    content {
      #checkov:skip=CKV_AWS_356:comprehend:DetectPiiEntities supports no resource-level permissions.
      sid       = statement.value
      actions   = ["comprehend:DetectPiiEntities"]
      resources = ["*"]
    }
  }

  dynamic "statement" {
    for_each = var.pii_screening ? ["SourcePiiPolicy"] : []
    content {
      sid       = statement.value
      actions   = ["ssm:GetParameter"]
      resources = [local.source_access_parameter_arn]
    }
  }
}

data "aws_iam_policy_document" "chunk" {
  source_policy_documents = [
    data.aws_iam_policy_document.status_rw.json,
    data.aws_iam_policy_document.staging_rw.json,
  ]
}

data "aws_iam_policy_document" "embed" {
  source_policy_documents = [
    data.aws_iam_policy_document.status_rw.json,
    data.aws_iam_policy_document.staging_rw.json,
  ]

  # Each embedding profile's model (embedding.tf): Bedrock models, and the API keys of
  # OpenAI-compatible endpoints. Neither statement exists without such a profile.
  dynamic "statement" {
    for_each = length(local.bedrock_embedding_model_arns) > 0 ? ["Bedrock"] : []
    content {
      sid       = statement.value
      actions   = ["bedrock:InvokeModel"]
      resources = local.bedrock_embedding_model_arns
    }
  }

  dynamic "statement" {
    for_each = length(local.embedding_api_key_secret_arns) > 0 ? ["EmbeddingApiKeys"] : []
    content {
      sid       = statement.value
      actions   = ["secretsmanager:GetSecretValue"]
      resources = local.embedding_api_key_secret_arns
    }
  }

  statement {
    sid       = "EmbeddingCache"
    actions   = ["dynamodb:BatchGetItem", "dynamodb:BatchWriteItem", "dynamodb:PutItem"]
    resources = [module.cache_table.arn]
  }
}

data "aws_iam_policy_document" "validate" {
  source_policy_documents = [
    data.aws_iam_policy_document.status_rw.json,
    data.aws_iam_policy_document.staging_rw.json,
  ]
}

data "aws_iam_policy_document" "index" {
  source_policy_documents = [
    data.aws_iam_policy_document.status_rw.json,
    data.aws_iam_policy_document.staging_rw.json,
    # The tenant, source and metadata are written from the article's current row.
    data.aws_iam_policy_document.producer_rows_read.json,
  ]

  # A delete removes every version of the article's staged files ...
  statement {
    sid       = "PurgeStagingList"
    actions   = ["s3:ListBucketVersions"]
    resources = [module.staging.arn]
  }

  statement {
    sid       = "PurgeStagingVersions"
    actions   = ["s3:DeleteObjectVersion"]
    resources = ["${module.staging.arn}/*"]
  }

  # ... and its embedding-cache entries.
  statement {
    sid       = "PurgeEmbeddingCache"
    actions   = ["dynamodb:BatchWriteItem", "dynamodb:DeleteItem"]
    resources = [module.cache_table.arn]
  }

  statement {
    sid       = "ConnectAsWriter"
    actions   = ["rds-db:connect"]
    resources = ["${local.db_connect_prefix}${local.db_writer}"]
  }
}

data "aws_iam_policy_document" "finalize" {
  source_policy_documents = [
    data.aws_iam_policy_document.status_rw.json,
    data.aws_iam_policy_document.staging_rw.json,
    data.aws_iam_policy_document.producer_rows_read.json,
    data.aws_iam_policy_document.events.json,
  ]
}

# Permanent failures are a metric (ArticlesFailedPermanent) and the reconciler's hourly digest,
# so MarkFailed doesn't publish to the alert topic.
data "aws_iam_policy_document" "mark_failed" {
  source_policy_documents = [
    data.aws_iam_policy_document.status_rw.json,
    data.aws_iam_policy_document.producer_rows_read.json,
    data.aws_iam_policy_document.events.json,
  ]
}

data "aws_iam_policy_document" "migrate" {
  statement {
    sid       = "MasterSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [local.db_master_secret]
  }

  statement {
    sid       = "MasterSecretKey"
    actions   = ["kms:Decrypt"]
    resources = [module.kms.key_arn]
  }
}

# -- OCR (Textract async, brief §4.4) -------------------------------------------------
# Textract reads the producer's object and writes its result to staging with the caller's
# permissions (forward access sessions), so ocr_start holds both.

data "aws_iam_policy_document" "ocr_start" {
  source_policy_documents = [
    data.aws_iam_policy_document.status_rw.json,
    data.aws_iam_policy_document.staging_rw.json,
    data.aws_iam_policy_document.producer_raw_read.json,
  ]

  statement {
    #checkov:skip=CKV_AWS_356:Textract job APIs don't support resource-level permissions.
    sid       = "StartDocumentAnalysis" # LAYOUT + TABLES (extract/ocr.py)
    actions   = ["textract:StartDocumentAnalysis"]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "ocr_poll" {
  source_policy_documents = [data.aws_iam_policy_document.status_rw.json]

  statement {
    #checkov:skip=CKV_AWS_356:Textract job APIs don't support resource-level permissions.
    sid       = "DocumentAnalysisStatus"
    actions   = ["textract:GetDocumentAnalysis"]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "ocr_collect" {
  source_policy_documents = [
    data.aws_iam_policy_document.status_rw.json,
    data.aws_iam_policy_document.staging_rw.json,
  ]
}

# -- operations (control API, reconciler) ------------------------------------------

data "aws_iam_policy_document" "status_read" {
  statement {
    sid       = "StatusTableRead"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [module.status_table.arn, module.status_table.index_arns]
  }

  statement {
    sid       = "IngestionKeyRead"
    actions   = ["kms:Decrypt"]
    resources = [module.kms.key_arn]
  }
}

# Re-runs: read the staged outputs' ETags (resume) and put messages on the ingest queue.
data "aws_iam_policy_document" "requeue" {
  statement {
    sid       = "Enqueue"
    actions   = ["sqs:SendMessage"]
    resources = [module.ingest_queue.queue_arn]
  }

  statement {
    sid       = "ResumeHead" # HeadObject needs s3:GetObject
    actions   = ["s3:GetObject"]
    resources = ["${module.staging.arn}/*"]
  }

  statement {
    sid       = "ResumeList" # 404 instead of 403 for a missing staged file
    actions   = ["s3:ListBucket"]
    resources = [module.staging.arn]
  }
}

data "aws_iam_policy_document" "control_api" {
  source_policy_documents = [
    data.aws_iam_policy_document.status_read.json,
    data.aws_iam_policy_document.requeue.json,
    data.aws_iam_policy_document.producer_rows_read.json,
  ]

  # Redrive: move the ingest DLQ back onto the ingest queue.
  statement {
    sid       = "RedriveSource"
    actions   = ["sqs:StartMessageMoveTask", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [module.ingest_queue.dlq_arn]
  }

  statement {
    sid       = "RedriveTarget"
    actions   = ["sqs:GetQueueAttributes"]
    resources = [module.ingest_queue.queue_arn]
  }

  # GET /v1/ops/overview: the backfill lane's queue depths (the ingest queues are above).
  statement {
    sid       = "BackfillQueueDepths"
    actions   = ["sqs:GetQueueAttributes"]
    resources = [module.backfill_queue.queue_arn, module.backfill_queue.dlq_arn]
  }

  statement {
    sid       = "StartReindex"
    actions   = ["states:StartExecution"]
    resources = [local.reindex_state_machine_arn]
  }

  statement {
    sid       = "TableOperations" # status / switch chunks_active, run by the tables function
    actions   = ["lambda:InvokeFunction"]
    resources = local.tables_invoke_arns
  }

  # POST /v1/search: who may search which tenant, the query embedding, the search itself.
  statement {
    sid       = "SourceAccessPolicy"
    actions   = ["ssm:GetParameter"]
    resources = [local.source_access_parameter_arn]
  }

  # Each embedding profile's model (embedding.tf): Bedrock models, and the API keys of
  # OpenAI-compatible endpoints. Neither statement exists without such a profile.
  dynamic "statement" {
    for_each = length(local.bedrock_embedding_model_arns) > 0 ? ["EmbedQuery"] : []
    content {
      sid       = statement.value
      actions   = ["bedrock:InvokeModel"]
      resources = local.bedrock_embedding_model_arns
    }
  }

  dynamic "statement" {
    for_each = length(local.embedding_api_key_secret_arns) > 0 ? ["EmbeddingApiKeys"] : []
    content {
      sid       = statement.value
      actions   = ["secretsmanager:GetSecretValue"]
      resources = local.embedding_api_key_secret_arns
    }
  }

  statement {
    sid       = "Search" # hybrid search inside the VPC, as ingest_reader
    actions   = ["lambda:InvokeFunction"]
    resources = local.query_invoke_arns
  }

  # Optional reranking (rerank_enabled). Rerank has no resource types; the model it may use is
  # scoped by InvokeModel.
  dynamic "statement" {
    for_each = var.rerank_enabled ? ["Rerank"] : []
    content {
      #checkov:skip=CKV_AWS_356:bedrock:Rerank supports no resource-level permissions.
      sid       = statement.value
      actions   = ["bedrock:Rerank"]
      resources = ["*"]
    }
  }

  dynamic "statement" {
    for_each = var.rerank_enabled ? ["RerankModel"] : []
    content {
      sid       = statement.value
      actions   = ["bedrock:InvokeModel"]
      resources = [local.rerank_model_arn]
    }
  }

  # The vector-table registry (#CONFIG/VECTOR_TABLES): write tables and the live table.
  statement {
    sid       = "VectorTables"
    actions   = ["dynamodb:PutItem"]
    resources = [module.status_table.arn]

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:LeadingKeys"
      values   = ["#CONFIG"]
    }
  }
}

data "aws_iam_policy_document" "reindex_plan" {
  # The producer's rows: the tenant of articles indexed before LATEST recorded it.
  source_policy_documents = [
    data.aws_iam_policy_document.status_read.json,
    data.aws_iam_policy_document.producer_rows_read.json,
  ]

  statement {
    sid       = "WritePlan"
    actions   = ["s3:PutObject"]
    resources = ["${module.staging.arn}/_reindex/*"]
  }

  statement {
    sid       = "StagingKey"
    actions   = ["kms:GenerateDataKey"]
    resources = [module.kms.key_arn]
  }
}

data "aws_iam_policy_document" "reindex_enqueue" {
  source_policy_documents = [
    data.aws_iam_policy_document.status_read.json,
    data.aws_iam_policy_document.producer_rows_read.json,
  ]

  statement {
    sid       = "EnqueueBackfill"
    actions   = ["sqs:SendMessage"]
    resources = [module.backfill_queue.queue_arn]
  }
}

# Read-only pgvector access (inside the VPC) for search, evaluation and the drift check.
data "aws_iam_policy_document" "query" {
  statement {
    sid       = "ConnectAsReader"
    actions   = ["rds-db:connect"]
    resources = ["${local.db_connect_prefix}${local.db_reader}"]
  }
}

# Vector-table operations (status, switch chunks_active) as ingest_ops: no master credentials.
data "aws_iam_policy_document" "tables" {
  statement {
    sid       = "ConnectAsOps"
    actions   = ["rds-db:connect"]
    resources = ["${local.db_connect_prefix}${local.db_ops}"]
  }
}

data "aws_iam_policy_document" "eval" {
  # LATEST: drop golden questions whose article was deleted or changed version.
  source_policy_documents = [data.aws_iam_policy_document.status_read.json]

  # Embed the golden questions.
  dynamic "statement" {
    for_each = length(local.bedrock_embedding_model_arns) > 0 ? ["Bedrock"] : []
    content {
      sid       = statement.value
      actions   = ["bedrock:InvokeModel"]
      resources = local.bedrock_embedding_model_arns
    }
  }

  dynamic "statement" {
    for_each = length(local.embedding_api_key_secret_arns) > 0 ? ["EmbeddingApiKeys"] : []
    content {
      sid       = statement.value
      actions   = ["secretsmanager:GetSecretValue"]
      resources = local.embedding_api_key_secret_arns
    }
  }

  statement {
    sid       = "GoldenSet" # versioned sets + LATEST; the article-only set from before them
    actions   = ["s3:GetObject"]
    resources = ["${module.staging.arn}/_eval/golden/*", "${module.staging.arn}/_eval/golden.jsonl"]
  }

  statement {
    sid       = "GoldenSetMissing" # 404 (not 403) when there's no LATEST yet
    actions   = ["s3:ListBucket"]
    resources = [module.staging.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["_eval/golden/*", "_eval/golden.jsonl"]
    }
  }

  statement {
    sid       = "Reports" # _eval/<table>/<time>.json; never the golden sets
    actions   = ["s3:PutObject"]
    resources = ["${module.staging.arn}/_eval/chunks_*"]
  }

  statement {
    sid       = "StagingKey"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [module.kms.key_arn]
  }

  statement {
    sid       = "Search"
    actions   = ["lambda:InvokeFunction"]
    resources = local.query_invoke_arns
  }
}

data "aws_iam_policy_document" "reconciler" {
  source_policy_documents = [
    data.aws_iam_policy_document.status_rw.json,
    data.aws_iam_policy_document.requeue.json,
    data.aws_iam_policy_document.producer_rows_read.json,
    data.aws_iam_policy_document.events.json,
  ]

  statement {
    sid     = "ProducerUpdatedIndex" # read-only: drift check over gsi_updated(_shard)
    actions = ["dynamodb:Query"]
    resources = [
      "${local.producer.articles_table_arn}/index/gsi_updated",
      "${local.producer.articles_table_arn}/index/gsi_updated_shard",
    ]
  }

  statement {
    sid       = "Alert"
    actions   = ["sns:Publish"]
    resources = [module.alerts.topic_arn]
  }

  statement {
    sid       = "ChunkCounts" # weekly drift check (brief §11.6)
    actions   = ["lambda:InvokeFunction"]
    resources = local.query_invoke_arns
  }

  statement {
    sid       = "DriftRepairs" # shadow runs on the low-priority queue
    actions   = ["sqs:SendMessage"]
    resources = [module.backfill_queue.queue_arn]
  }
}

# -- workflows ----------------------------------------------------------------------

data "aws_iam_policy_document" "reindex_workflow" {
  statement {
    sid       = "InvokeReindexFunctions"
    actions   = ["lambda:InvokeFunction"]
    resources = flatten([for name in ["reindex_plan", "reindex_enqueue"] : [module.fn[name].function_arn, module.fn[name].alias_arn]])
  }

  statement {
    sid       = "ReadPlan" # the Distributed Map's ItemReader
    actions   = ["s3:GetObject"]
    resources = ["${module.staging.arn}/_reindex/*"]
  }

  statement {
    sid       = "StackKey" # read the plan; encrypt the Distributed Map's child execution data
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [module.kms.key_arn]
  }

  statement {
    sid       = "MapChildren" # Distributed Map runs each batch as a child workflow execution
    actions   = ["states:StartExecution"]
    resources = [local.reindex_state_machine_arn]
  }

  statement {
    sid       = "MapRuns"
    actions   = ["states:DescribeExecution", "states:StopExecution"]
    resources = ["arn:${local.partition}:states:${local.region}:${local.account_id}:execution:${local.reindex_state_machine_name}/*"]
  }
}

data "aws_iam_policy_document" "ingest_workflow" {
  statement {
    sid       = "InvokeSteps"
    actions   = ["lambda:InvokeFunction"]
    resources = flatten([for name in local.workflow_functions : [module.fn[name].function_arn, module.fn[name].alias_arn]])
  }
}

# The permissions boundary of every IAM role the stack creates (plan assertions). A new module
# that creates a role belongs here.
locals {
  role_permissions_boundaries = flatten(concat(
    [for f in module.fn : f.role_permissions_boundaries],
    [
      module.vpc.role_permissions_boundaries,
      module.pipe.role_permissions_boundaries,
      module.ingest_workflow.role_permissions_boundaries,
      module.reindex_workflow.role_permissions_boundaries,
      module.reconcile_schedule.role_permissions_boundaries,
      module.drift_schedule.role_permissions_boundaries,
      module.control_api.role_permissions_boundaries,
    ],
    module.db[*].role_permissions_boundaries,
    module.proxy[*].role_permissions_boundaries,
    module.rds[*].role_permissions_boundaries,
    module.backup[*].role_permissions_boundaries,
  ))
}

# Plan-time warning if a role misses the boundary (the deploy role's CreateRole would fail).
check "roles_carry_the_workload_boundary" {
  assert {
    condition     = alltrue([for b in local.role_permissions_boundaries : coalesce(b, "none") == coalesce(local.permissions_boundary, "none")])
    error_message = "A role in this stack doesn't carry the workload boundary (local.permissions_boundary)."
  }
}
