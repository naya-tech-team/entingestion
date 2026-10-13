# Plan-time assertions for the ingestion stack.
mock_provider "aws" {
  override_during = plan

  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-west-2" }
  }
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-west-2a", "us-west-2b", "us-west-2c"] }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_data "aws_dynamodb_table" {
    defaults = { arn = "arn:aws:dynamodb:us-west-2:123456789012:table/producer", name = "producer" }
  }
  mock_data "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::producer-raw" }
  }
  mock_data "aws_ssm_parameter" {
    defaults = { value = "arn:aws:kms:us-west-2:123456789012:key/producer" }
  }
  mock_data "aws_ec2_managed_prefix_list" {
    defaults = { id = "pl-123" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-west-2:123456789012:key/ingestion" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::ingest-staging" }
  }
  mock_resource "aws_dynamodb_table" {
    defaults = { arn = "arn:aws:dynamodb:us-west-2:123456789012:table/ingestion" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = { arn = "arn:aws:sqs:us-west-2:123456789012:q" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:us-west-2:123456789012:function:fn", version = "1" }
  }
  mock_resource "aws_lambda_alias" {
    defaults = {
      arn        = "arn:aws:lambda:us-west-2:123456789012:function:fn:live"
      invoke_arn = "arn:aws:apigateway:us-west-2:lambda:path/2015-03-31/functions/arn:aws:lambda:us-west-2:123456789012:function:fn:live/invocations"
    }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-west-2:123456789012:log-group:lg" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-west-2:123456789012:alerts" }
  }
  mock_resource "aws_api_gateway_rest_api" {
    defaults = { id = "abc123", execution_arn = "arn:aws:execute-api:us-west-2:123456789012:abc123" }
  }
  mock_resource "aws_db_proxy" {
    defaults = { arn = "arn:aws:rds:us-west-2:123456789012:db-proxy:prx-abc", endpoint = "proxy.example" }
  }
  mock_resource "aws_db_instance" {
    defaults = {
      arn                = "arn:aws:rds:us-west-2:123456789012:db:ingestion-dev"
      resource_id        = "db-RDS"
      address            = "rds.example"
      master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:us-west-2:123456789012:secret:rds-instance" }]
    }
  }
  mock_resource "aws_rds_cluster" {
    defaults = {
      id                  = "ingestion-dev"
      arn                 = "arn:aws:rds:us-west-2:123456789012:cluster:ingestion-dev"
      cluster_resource_id = "cluster-XYZ"
      endpoint            = "writer.example"
      master_user_secret  = [{ secret_arn = "arn:aws:secretsmanager:us-west-2:123456789012:secret:rds" }]
    }
  }
}

# The backup copy region (backup_enabled).
mock_provider "aws" {
  alias           = "backup_copy"
  override_during = plan

  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-2:123456789012:key/copy" }
  }
  mock_resource "aws_backup_vault" {
    defaults = { arn = "arn:aws:backup:us-east-2:123456789012:backup-vault:copy" }
  }
}

variables {
  env            = "dev"
  region         = "us-west-2"
  aws_account_id = "123456789012"
  mandatory_tags = {
    created_by           = "test"
    created_date         = "2026-01-01"
    owner                = "test"
    internal_client_code = "test"
    client               = "test"
    project_name         = "test"
    division             = "test"
  }
  lambda_core_zip    = "tests/fixtures/core.zip"
  lambda_docs_zip    = "tests/fixtures/docs.zip"
  backup_copy_region = null

  # What envs/<env>/ingestion.tfvars set (the variables have no defaults).
  embedding_profiles = {
    primary = { provider = "bedrock", model = "amazon.titan-embed-text-v2:0", dimension = 1024 }
    compact = { provider = "bedrock", model = "amazon.titan-embed-text-v2:0", dimension = 512 }
  }
  default_profile = "primary"
  rerank_model_id = "amazon.rerank-v1:0"
}

run "pipe_filters_the_articles_stream" {
  command = plan

  assert {
    condition = (
      length(module.pipe.settings.filter_patterns) == 2
      && module.pipe.settings.starting_position == "LATEST"
      && module.pipe.settings.max_retries == 5
    )
    error_message = "Pipe must use the two filters (STAGED rows, REMOVEs), start at LATEST and retry 5 times before its DLQ."
  }

  assert {
    condition = contains(
      [for p in module.pipe.settings.filter_patterns : jsondecode(p).eventName],
      ["REMOVE"],
    )
    error_message = "REMOVE events must reach the delete path."
  }
}

run "status_table_has_the_four_sparse_gsis" {
  command = plan

  assert {
    condition = (
      toset([for g in module.status_table.settings.global_secondary_index : g.name])
      == toset(["gsi1-status", "gsi2-process", "gsi3-lease", "gsi4-stepfail"])
      && module.status_table.settings.ttl_attribute == "expires_at_epoch"
      && module.status_table.settings.range_key == "sk"
    )
    error_message = "ingestion_status must have pk/sk, the four sparse GSIs and TTL."
  }
}

run "only_database_functions_run_in_the_vpc" {
  command = plan

  assert {
    condition     = toset([for k, f in module.fn : k if f.settings.in_vpc]) == toset(["index", "migrate", "query", "tables"])
    error_message = "Only the database functions (index, migrate, query, tables) run in the VPC."
  }

  assert {
    condition = alltrue([
      for k, f in module.fn :
      f.settings.architectures == tolist(["arm64"]) && f.settings.runtime == "python3.12"
    ])
    error_message = "Every function must be python3.12 on arm64."
  }

  assert {
    condition     = module.fn["chunk"].settings.environment.TIKTOKEN_CACHE_DIR == "/var/task/tiktoken_cache"
    error_message = "Chunk must use the bundled tokenizer encoding (no internet in Lambda)."
  }

  assert {
    condition     = toset([for o in aws_s3_object.package : o.key]) == toset(["lambda/core.zip", "lambda/docs.zip"])
    error_message = "Both packages must be deployed through S3 (they exceed the direct-upload limit)."
  }
}

run "producer_access_is_read_only" {
  command = plan

  assert {
    condition = alltrue([
      for s in concat(
        data.aws_iam_policy_document.producer_raw_read.statement,
        data.aws_iam_policy_document.producer_rows_read.statement,
        data.aws_iam_policy_document.pipe_producer_key.statement,
      ) :
      alltrue([for a in s.actions : contains(["s3:GetObject", "s3:GetObjectVersion", "kms:Decrypt", "dynamodb:GetItem", "dynamodb:BatchGetItem"], a)])
    ])
    error_message = "The pipeline may only read producer data."
  }
}

run "no_wildcard_resources_in_function_policies" {
  command = plan

  assert {
    condition = alltrue(flatten([
      for doc in [
        data.aws_iam_policy_document.status_rw, data.aws_iam_policy_document.staging_rw,
        data.aws_iam_policy_document.dispatcher, data.aws_iam_policy_document.embed,
        data.aws_iam_policy_document.index, data.aws_iam_policy_document.mark_failed,
        data.aws_iam_policy_document.migrate, data.aws_iam_policy_document.control_api,
        data.aws_iam_policy_document.reconciler, data.aws_iam_policy_document.requeue,
        data.aws_iam_policy_document.status_read,
      ] : [for s in doc.statement : [for r in s.resources : r != "*"]]
    ]))
    error_message = "Function policies must name their resources."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.index.statement :
      s.sid == "ConnectAsWriter" && s.resources == toset(["arn:aws:rds-db:us-west-2:123456789012:dbuser:prx-abc/ingest_writer"])
    ])
    error_message = "Index may only connect to the proxy as ingest_writer."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.migrate.statement :
      s.sid == "RegistryRead" && toset(s.actions) == toset(["dynamodb:GetItem"])
      && length(s.resources) == 1
      && anytrue([for c in s.condition : c.variable == "dynamodb:LeadingKeys" && toset(c.values) == toset(["#CONFIG"])])
    ])
    error_message = "migrate may only GetItem the status table's #CONFIG items (the vector-table registry)."
  }

  assert {
    condition = length([
      for s in data.aws_iam_policy_document.migrate.statement : s
      if length(setintersection(s.actions, ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:*"])) > 0
    ]) == 0
    error_message = "migrate never writes, queries or scans DynamoDB."
  }
}

run "database_uses_end_to_end_iam" {
  command = plan

  assert {
    condition     = module.proxy[0].settings.default_auth_scheme == "IAM_AUTH" && module.proxy[0].settings.require_tls
    error_message = "RDS Proxy must use end-to-end IAM auth over TLS."
  }

  assert {
    condition     = module.db[0].settings.iam_auth && module.db[0].settings.storage_encrypted && !module.db[0].settings.deletion_protection
    error_message = "dev cluster: encrypted, IAM auth, deletion protection off."
  }
}

run "workflow_wires_every_step" {
  command = plan

  assert {
    condition = (
      module.ingest_workflow.settings.definition_start == "Claim"
      && module.ingest_workflow.settings.type == "STANDARD"
    )
    error_message = "Workflow must be STANDARD and start at Claim."
  }

  assert {
    condition = alltrue([
      for state in ["AfterFetch", "AfterExtract", "OcrStart", "OcrWait", "OcrPoll", "OcrDone", "OcrCollect"] :
      contains(module.ingest_workflow.settings.definition_states, state)
    ])
    error_message = "Scanned and large PDFs must have the OCR branch (UC12)."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.ocr_start.statement :
      s.sid != "StartDocumentAnalysis" || tolist(s.actions) == tolist(["textract:StartDocumentAnalysis"])
    ])
    error_message = "ocr_start may only start document analysis jobs."
  }

  assert {
    condition = (
      anytrue([
        for c in jsondecode(local.ingest_definition).States.Route.Choices :
        try(c.Variable, "") == "$.ctx.action" && try(c.StringEquals, "") == "METADATA" && c.Next == "Index"
      ])
      && anytrue([
        for c in jsondecode(local.ingest_definition).States.AfterIndex.Choices :
        c.Variable == "$.ctx.mode" && c.StringEquals == "METADATA" && c.Next == "Done"
      ])
    )
    error_message = "A metadata-only run (Claim action METADATA) must go to Index and end there."
  }
}

run "prod_is_protected" {
  command = plan

  variables {
    env          = "prod"
    alarm_emails = ["oncall@example.com"]
  }

  assert {
    condition = (
      module.db[0].settings.deletion_protection
      && !module.db[0].settings.skip_final_snapshot
      && module.status_table.settings.deletion_protection
      && module.cache_table.settings.deletion_protection
      && !module.staging.settings.force_destroy
    )
    error_message = "prod: deletion protection on the cluster and tables, a final snapshot, and no force_destroy on staging."
  }
}

run "free_plan_rds_mode" {
  command = plan

  variables {
    database_mode            = "rds"
    db_backup_retention_days = 1
    db_performance_insights  = false
    db_monitoring_interval   = 0
  }

  assert {
    condition     = length(module.db) == 0 && length(module.proxy) == 0 && length(module.rds) == 1
    error_message = "rds mode: one RDS instance, no Aurora, no RDS Proxy (not on the free plan)."
  }

  assert {
    condition = (
      module.rds[0].settings.backup_retention_days == 1
      && !module.rds[0].settings.publicly_accessible
      && module.rds[0].settings.iam_auth
      && module.rds[0].settings.instance_class == "db.t4g.micro"
    )
    error_message = "rds mode must fit the free plan and stay private with IAM auth."
  }

  assert {
    condition = (
      module.fn["index"].settings.environment.DB_HOST == "rds.example"
      && module.fn["index"].settings.environment.DB_SSLROOTCERT == "/var/task/certs/rds-global-bundle.pem"
    )
    error_message = "Index must connect straight to the instance and verify it with the RDS CA bundle."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.index.statement :
      s.sid == "ConnectAsWriter" && s.resources == toset(["arn:aws:rds-db:us-west-2:123456789012:dbuser:db-RDS/ingest_writer"])
    ])
    error_message = "Index may only connect to the instance as ingest_writer."
  }
}

run "operations_functions_are_wired" {
  command = plan

  assert {
    condition = (
      module.fn["control_api"].settings.environment.INGEST_QUEUE_URL != null
      && module.fn["control_api"].settings.environment.BACKFILL_DLQ_URL != null
      && module.fn["reconciler"].settings.environment.ALERT_TOPIC_ARN != null
      && module.fn["reconciler"].settings.environment.RECONCILE_BATCH == "500"
    )
    error_message = "Control API and reconciler need the ingest queue (the control API also the backfill DLQ, for its overview); the reconciler also the alert topic."
  }

  assert {
    condition = (
      module.reconcile_schedule.settings.schedule_expression == "rate(1 hour)"
      && module.reconcile_schedule.settings.state == "ENABLED"
    )
    error_message = "The reconciler runs hourly."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.reconciler.statement :
      s.sid != "ProducerUpdatedIndex" || (tolist(s.actions) == tolist(["dynamodb:Query"]) && s.resources == toset(["arn:aws:dynamodb:us-west-2:123456789012:table/producer/index/gsi_updated_shard"]))
    ])
    error_message = "The reconciler may only query the producer's gsi_updated_shard index."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.control_api.statement :
      s.sid == "VectorTables" || alltrue([for a in s.actions : !startswith(a, "dynamodb:") || contains(["dynamodb:GetItem", "dynamodb:Query"], a)])
    ])
    error_message = "The control API only reads DynamoDB; changes go through the queue."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.control_api.statement :
      s.sid != "VectorTables" || (
        tolist(s.actions) == tolist(["dynamodb:PutItem"])
        && one([for c in s.condition : c.variable]) == "dynamodb:LeadingKeys"
        && tolist(one([for c in s.condition : c.values])) == tolist(["#CONFIG"])
      )
    ])
    error_message = "The control API writes only the #CONFIG items (the vector-table registry)."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.control_api.statement :
      s.sid == "BackfillQueueDepths"
      && tolist(s.actions) == tolist(["sqs:GetQueueAttributes"])
      && toset(s.resources) == toset([module.backfill_queue.queue_arn, module.backfill_queue.dlq_arn])
    ])
    error_message = "The control API reads the backfill queue's and backfill DLQ's depths (GET /v1/ops/overview), nothing more."
  }
}

run "control_api_splits_readers_and_operators" {
  command = plan

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.control_reader.statement :
      alltrue([for r in s.resources : strcontains(r, "/GET/v1/")])
    ])
    error_message = "The reader policy must only allow GET routes."
  }

  assert {
    condition = (
      module.control_api.stage_name == "api"
      && module.control_api.settings.proxy_authorization == "AWS_IAM"
      && length(module.waf) == 1
    )
    error_message = "The control API uses IAM auth and has a WAF ACL by default."
  }
}

run "publishes_the_control_api_for_the_ui" {
  command = plan

  assert {
    condition = (
      length(aws_ssm_parameter.published) == 2
      && aws_ssm_parameter.published["control_api_url"].name == "/entingestion/dev/ingestion/control_api_url"
      && aws_ssm_parameter.published["control_api_execution_arn"].name == "/entingestion/dev/ingestion/control_api_execution_arn"
      && aws_ssm_parameter.published["control_api_execution_arn"].value == "arn:aws:execute-api:us-west-2:123456789012:abc123"
    )
    error_message = "Ingestion must publish the control API's URL and execution ARN under /entingestion/<env>/ingestion/."
  }
}

run "search_is_wired_and_reranking_is_off_by_default" {
  command = plan

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.control_search.statement :
      alltrue([for r in s.resources : endswith(r, "/api/POST/v1/search")])
    ])
    error_message = "The search policy must only allow POST /v1/search."
  }

  assert {
    condition = (
      module.fn["control_api"].settings.environment.SOURCE_ACCESS_PARAMETER == "/entingestion/dev/producer/source_access"
      && module.fn["control_api"].settings.environment.QUERY_FUNCTION_NAME == "ingestion-dev-query:live"
      && module.fn["control_api"].settings.environment.RERANK_ENABLED == "false"
    )
    error_message = "The control API needs the source access parameter and the query function's live alias; rerank is off."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.control_api.statement : !contains(tolist(s.actions), "bedrock:Rerank")
    ])
    error_message = "Without rerank_enabled the control API may not call Rerank."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.control_api.statement :
      s.sid == "SourceAccessPolicy" && s.resources == toset(["arn:aws:ssm:us-west-2:123456789012:parameter/entingestion/dev/producer/source_access"])
    ])
    error_message = "The control API reads only the producer's source access parameter."
  }
}

run "reranking_can_be_switched_on" {
  command = plan

  variables {
    rerank_enabled = true
  }

  assert {
    condition = (
      module.fn["control_api"].settings.environment.RERANK_ENABLED == "true"
      && anytrue([
        for s in data.aws_iam_policy_document.control_api.statement :
        s.sid == "RerankModel" && s.resources == toset(["arn:aws:bedrock:us-west-2::foundation-model/amazon.rerank-v1:0"])
      ])
      # Rerank has no resource types: its statement is the only wildcard.
      && alltrue([
        for s in data.aws_iam_policy_document.control_api.statement :
        s.sid == "Rerank" || alltrue([for r in s.resources : r != "*"])
      ])
    )
    error_message = "rerank_enabled must allow Rerank and InvokeModel on the reranking model only."
  }
}

run "alarms_cover_the_pipeline_and_the_database" {
  command = plan

  assert {
    condition = alltrue([for k in [
      "ingest-dlq-new-messages", "pipe-dlq-new-messages", "ingest-queue-backlog", "runs-failing",
      "bedrock-throttled", "freshness-lag", "backlog-age", "failure-ratio", "aurora-cpu",
      "aurora-capacity-near-max", "aurora-buffer-cache", "proxy-connections",
    ] : contains(keys(module.alerts.settings), k)])
    error_message = "Aurora mode must alarm on DLQs, backlog, failures, Bedrock throttles, freshness and the database."
  }

  assert {
    condition     = !contains(keys(module.alerts.settings), "db-free-storage")
    error_message = "Instance-only alarms don't apply in aurora mode."
  }
}

run "alarms_follow_the_database_mode" {
  command = plan

  variables {
    database_mode            = "rds"
    db_backup_retention_days = 1
  }

  assert {
    condition = (
      contains(keys(module.alerts.settings), "db-free-storage")
      && contains(keys(module.alerts.settings), "db-cpu")
      && !contains(keys(module.alerts.settings), "proxy-connections")
    )
    error_message = "rds mode alarms on the instance, not on Aurora or the proxy."
  }
}

run "ocr_can_be_switched_off" {
  command = plan

  variables {
    ocr_enabled = false
  }

  assert {
    condition     = module.fn["fetch"].settings.environment.OCR_ENABLED == "false" && module.fn["extract"].settings.environment.OCR_ENABLED == "false"
    error_message = "ocr_enabled = false must reach Fetch and Extract."
  }
}

run "pii_screening_is_off_by_default" {
  command = plan

  assert {
    condition = (
      module.fn["normalize"].settings.environment.PII_SCREENING == "false"
      && alltrue([for s in data.aws_iam_policy_document.normalize.statement : !contains(tolist(s.actions), "comprehend:DetectPiiEntities")])
    )
    error_message = "Without pii_screening, Normalize neither screens nor may call Comprehend."
  }
}

run "pii_screening_can_be_switched_on" {
  command = plan

  variables {
    pii_screening = true
  }

  assert {
    condition = (
      module.fn["normalize"].settings.environment.PII_SCREENING == "true"
      && anytrue([for s in data.aws_iam_policy_document.normalize.statement : s.sid == "DetectPii" && tolist(s.actions) == tolist(["comprehend:DetectPiiEntities"])])
      && anytrue([for s in data.aws_iam_policy_document.normalize.statement : s.sid == "SourcePiiPolicy" && s.resources == toset(["arn:aws:ssm:us-west-2:123456789012:parameter/entingestion/dev/producer/source_access"])])
    )
    error_message = "pii_screening must allow DetectPiiEntities and reading the source policies."
  }
}

run "deleted_articles_leave_nothing_behind" {
  command = plan

  assert {
    condition = alltrue([
      for sid, actions in {
        PurgeStagingList     = ["s3:ListBucketVersions"]
        PurgeStagingVersions = ["s3:DeleteObjectVersion"]
        PurgeEmbeddingCache  = ["dynamodb:BatchWriteItem", "dynamodb:DeleteItem"]
      } : anytrue([for s in data.aws_iam_policy_document.index.statement : s.sid == sid && toset(s.actions) == toset(actions)])
    ])
    error_message = "Index's delete path must purge the article's staged versions and cache entries."
  }

  assert {
    condition = anytrue([
      for r in module.staging.settings.lifecycle_rules :
      r.id == "versions" && r.prefix == "" && r.noncurrent_version_expiration_days == 90 && r.expiration_days == null
    ])
    error_message = "Staging expires noncurrent versions (old article versions) and never the current one (the rebuild source)."
  }
}

run "noncurrent_staging_expires_after_its_transition" {
  command = plan

  variables {
    staging_noncurrent_days            = 30
    staging_noncurrent_expiration_days = 30
  }

  expect_failures = [var.staging_noncurrent_expiration_days]
}

run "reindex_workflow_is_wired" {
  command = plan

  assert {
    condition = (
      module.reindex_workflow.settings.definition_start == "Plan"
      && contains(module.reindex_workflow.settings.definition_states, "Enqueue")
    )
    error_message = "Reindex plans the articles, then queues them with a Distributed Map."
  }

  assert {
    condition = (
      module.fn["control_api"].settings.environment.REINDEX_STATE_MACHINE_ARN == "arn:aws:states:us-west-2:123456789012:stateMachine:ingestion-dev-reindex"
      && module.fn["control_api"].settings.environment.TABLES_FUNCTION_NAME == "ingestion-dev-tables:live"
      && module.fn["reindex_enqueue"].settings.environment.BACKFILL_QUEUE_URL != null
    )
    error_message = "The control API starts reindexing and switches tables through the tables function; batches go to the backfill queue."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.reindex_enqueue.statement :
      s.sid != "EnqueueBackfill" || tolist(s.resources) == tolist([module.backfill_queue.queue_arn])
    ])
    error_message = "Reindex batches may only go to the backfill queue."
  }
}

run "quality_and_drift_are_wired" {
  command = plan

  assert {
    condition     = module.fn["query"].settings.environment.DB_USER == "ingest_reader"
    error_message = "Search, evaluation and the drift check read as ingest_reader."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.query.statement :
      tolist(s.actions) == tolist(["rds-db:connect"]) && alltrue([for r in s.resources : endswith(r, "/ingest_reader")])
    ])
    error_message = "The query function may only connect as ingest_reader."
  }

  assert {
    condition = (
      module.drift_schedule.settings.schedule_expression == "rate(7 days)"
      && module.drift_schedule.settings.target_arn == module.fn["reconciler"].alias_arn
    )
    error_message = "The drift check runs weekly through the reconciler."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.eval.statement :
      s.sid == "Reports" && s.resources == toset(["arn:aws:s3:::ingest-staging/_eval/chunks_*"])
    ])
    error_message = "The eval function writes reports under _eval/<table>/ only, never the golden sets."
  }

  assert {
    condition = (
      module.fn["control_api"].settings.environment.EVAL_TOLERANCE == "0.02"
      && module.fn["control_api"].settings.environment.EVAL_MAX_AGE_DAYS == "7"
    )
    error_message = "The switch gate allows recall@10 0.02 below the live table, from a report under 7 days old."
  }
}

run "operate_alarms_and_dashboard" {
  command = plan

  assert {
    condition = alltrue([for s in ["fetch", "extract", "normalize", "chunk", "embed", "validate", "index", "finalize"] :
      module.alerts.settings["step-${s}-slow"].extended_statistic == "p95"
      && module.alerts.settings["step-${s}-slow"].datapoints_to_alarm == 3
    ])
    error_message = "Every step needs a p95 duration alarm over 3 of 3 periods."
  }

  assert {
    condition     = module.alerts.settings["step-index-slow"].threshold == var.step_p95_alarm_ms["INDEX"]
    error_message = "Step alarm thresholds come from step_p95_alarm_ms."
  }

  assert {
    condition = alltrue([for m in ["StepDuration_INDEX", "ExecutionsFailed", "ApproximateAgeOfOldestMessage", "InvocationThrottles", "Eval_recall_at_10", "Reconciler_escalated", "ServerlessDatabaseCapacity", "ClientConnections"] :
      contains(local.dashboard_metric_names, m)
    ])
    error_message = "The dashboard must cover flow, backlog, steps, Bedrock, quality, reconciler and Aurora."
  }
}

run "dashboard_follows_the_database_mode" {
  command = plan

  variables {
    database_mode            = "rds"
    db_backup_retention_days = 1
  }

  assert {
    condition = (
      contains(local.dashboard_metric_names, "FreeStorageSpace")
      && !contains(local.dashboard_metric_names, "ServerlessDatabaseCapacity")
      && !contains(local.dashboard_metric_names, "ClientConnections")
    )
    error_message = "rds mode shows the instance, not Aurora or the proxy."
  }
}

run "every_resource_gets_the_mandatory_tags" {
  command = plan

  assert {
    condition = module.common.default_tags == {
      "Name"                 = "ingestion-dev"
      "Created By"           = "test"
      "Created Date"         = "2026-01-01"
      "Owner"                = "test"
      "Internal Client Code" = "test"
      "Client"               = "test"
      "Project Name"         = "test"
      "Division"             = "test"
      "env"                  = "dev"
    }
    error_message = "The provider tags every resource with the 8 mandatory tags (Name falls back to the stack) and env."
  }

  assert {
    condition     = local.permissions_boundary == "arn:aws:iam::123456789012:policy/entingestion-dev-workload-boundary" && module.boundary_check.all_bounded
    error_message = "The workload boundary ARN is unchanged and every role carries it."
  }
}

run "a_missing_mandatory_tag_stops_the_plan" {
  command = plan

  variables {
    mandatory_tags = {
      created_by           = "test"
      created_date         = "2026-01-01"
      owner                = "test"
      internal_client_code = "test"
      client               = ""
      project_name         = "test"
      division             = "test"
    }
  }

  expect_failures = [var.mandatory_tags]
}

run "a_bad_created_date_stops_the_plan" {
  command = plan

  variables {
    mandatory_tags = {
      created_by           = "test"
      created_date         = "10/11/2026"
      owner                = "test"
      internal_client_code = "test"
      client               = "test"
      project_name         = "test"
      division             = "test"
    }
  }

  expect_failures = [var.mandatory_tags]
}

run "extra_tags_stay_within_the_s3_object_limit" {
  command = plan

  variables {
    tags = { a = "1", b = "2" }
  }

  expect_failures = [var.tags]
}

# -- M4 hardening -----------------------------------------------------------------------

run "every_role_carries_the_workload_boundary" {
  command = plan

  assert {
    condition = alltrue([
      for b in local.role_permissions_boundaries : b == "arn:aws:iam::123456789012:policy/entingestion-dev-workload-boundary"
    ])
    error_message = "Every role the stack creates must carry entingestion-<env>-workload-boundary (the deploy role can't create it otherwise)."
  }

  assert {
    # Functions + VPC flow logs, pipe, 2 workflows, 2 schedules, Aurora monitoring, RDS Proxy.
    condition     = length(local.role_permissions_boundaries) == length(module.fn) + 8
    error_message = "Every role-creating module must be listed (and pass the boundary)."
  }
}

run "every_role_carries_the_workload_boundary_in_rds_mode" {
  command = plan

  variables {
    database_mode            = "rds"
    db_backup_retention_days = 1
  }

  assert {
    condition = (
      module.rds[0].role_permissions_boundaries == ["arn:aws:iam::123456789012:policy/entingestion-dev-workload-boundary"]
      && alltrue([for f in module.fn : f.role_permissions_boundaries == ["arn:aws:iam::123456789012:policy/entingestion-dev-workload-boundary"]])
    )
    error_message = "rds mode: the monitoring role and every function role carry the boundary."
  }
}

run "workload_boundary_can_be_overridden_or_off" {
  command = plan

  variables {
    permissions_boundary_arn = "arn:aws:iam::123456789012:policy/custom"
  }

  assert {
    condition     = alltrue([for b in local.role_permissions_boundaries : b == "arn:aws:iam::123456789012:policy/custom"])
    error_message = "permissions_boundary_arn replaces the computed boundary."
  }
}

run "workload_boundary_off" {
  command = plan

  variables {
    workload_boundary_enabled = false
  }

  assert {
    condition     = alltrue([for b in local.role_permissions_boundaries : b == null])
    error_message = "workload_boundary_enabled = false leaves the roles unbounded."
  }
}

run "table_operations_run_as_ingest_ops" {
  command = plan

  assert {
    condition = (
      module.fn["tables"].settings.handler == "ingest.tables.handler"
      && module.fn["tables"].settings.environment.DB_USER == "ingest_ops"
      && module.fn["tables"].settings.in_vpc
    )
    error_message = "The tables function connects as ingest_ops from inside the VPC."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.tables.statement :
      tolist(s.actions) == tolist(["rds-db:connect"]) && alltrue([for r in s.resources : endswith(r, "/ingest_ops")])
    ])
    error_message = "The tables function may only connect as ingest_ops."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.control_api.statement :
      s.sid == "TableOperations" && s.resources == toset([
        "arn:aws:lambda:us-west-2:123456789012:function:ingestion-dev-tables:live",
      ])
    ])
    error_message = "The control API invokes the tables function's live alias, never migrate (master credentials)."
  }

  assert {
    condition     = module.fn["migrate"].settings.environment.ALLOW_TRUNCATE == "false"
    error_message = "Truncate is off unless an environment's tfvars turns it on (dev does)."
  }
}

run "prod_refuses_truncate_and_runs_two_aurora_instances" {
  command = plan

  variables {
    env                   = "prod"
    alarm_emails          = ["oncall@example.com"]
    allow_truncate        = false
    aurora_instance_count = 2
  }

  assert {
    condition     = module.fn["migrate"].settings.environment.ALLOW_TRUNCATE == "false"
    error_message = "allow_truncate = false must reach the migrate function."
  }

  assert {
    condition     = module.db[0].settings.instance_count == 2
    error_message = "aurora_instance_count must reach the cluster (writer + failover reader)."
  }
}

run "producer_key_only_through_its_services" {
  command = plan

  assert {
    condition = alltrue([
      for pair in [
        { doc = data.aws_iam_policy_document.producer_raw_read, service = "s3.us-west-2.amazonaws.com" },
        { doc = data.aws_iam_policy_document.producer_rows_read, service = "dynamodb.us-west-2.amazonaws.com" },
        { doc = data.aws_iam_policy_document.pipe_producer_key, service = "dynamodb.us-west-2.amazonaws.com" },
      ] :
      alltrue([
        for s in pair.doc.statement :
        !contains(s.actions, "kms:Decrypt") || anytrue([for c in s.condition : c.variable == "kms:ViaService" && c.values == tolist([pair.service])])
      ])
    ])
    error_message = "The producer's key may only be used through S3 (objects) or DynamoDB (rows, stream)."
  }
}

run "gateway_endpoints_reach_only_this_account" {
  command = plan

  assert {
    condition = alltrue([
      for svc in ["s3", "dynamodb"] :
      anytrue([
        for s in data.aws_iam_policy_document.endpoint[svc].statement :
        anytrue([for c in s.condition : c.variable == "aws:ResourceAccount" && c.values == tolist(["123456789012"])])
      ])
    ])
    error_message = "The S3 and DynamoDB gateway endpoints must be limited to this account's resources."
  }
}

run "rejects_zero_aurora_instances" {
  command = plan

  variables {
    aurora_instance_count = 0
  }

  expect_failures = [var.aurora_instance_count]
}

# -- M5 operations ------------------------------------------------------------------------

run "prod_refuses_to_plan_without_an_alert_subscriber" {
  command = plan

  variables {
    env          = "prod"
    alarm_emails = []
  }

  expect_failures = [var.alarm_emails]
}

run "alarms_notify_once_per_incident" {
  command = plan

  assert {
    condition = alltrue([
      for a in module.alerts.alarm_names : length(module.alerts.settings) > 0
    ]) && !var.alarm_notify_on_ok
    error_message = "Alarms notify on ALARM only by default."
  }

  assert {
    condition = alltrue([for q in ["ingest", "backfill", "pipe"] :
      module.alerts.settings["${q}-dlq-new-messages"].expression == "MAX([DIFF(visible), sent])"
      && module.alerts.settings["${q}-dlq-not-drained"].evaluation_periods == 24
    ])
    error_message = "Each DLQ alarms on new messages (redrive moves don't count as sent) and on a day undrained."
  }

  assert {
    condition = alltrue([
      for k, a in module.alerts.settings : !startswith(k, "reconciler-escalated")
    ])
    error_message = "Escalations are in the hourly digest, not an alarm of their own."
  }
}

run "slos_have_alarms" {
  command = plan

  assert {
    condition = (
      module.alerts.settings["freshness-lag"].extended_statistic == "p95"
      && module.alerts.settings["freshness-lag"].dimensions == tomap({ service = "ingestion", Trigger = "STREAM" })
      && module.alerts.settings["freshness-lag"].threshold == 900
      && module.alerts.settings["backlog-age"].dimensions["status"] == "RUNNING"
      && module.alerts.settings["backlog-age"].threshold == 1800
      && module.alerts.settings["failure-ratio"].threshold == 1
      && length(module.alerts.settings["failure-ratio"].metrics) == 2
    )
    error_message = "Freshness p95 < 15 min (stream runs), backlog age < 30 min and failures < 1% each need an alarm."
  }

  assert {
    condition     = alltrue([for m in ["BacklogCount", "BacklogOldestSeconds", "FreshnessLagSeconds", "Dispatch_waiting_live"] : contains(local.dashboard_metric_names, m)])
    error_message = "The dashboard shows the SLO metrics and admission waits."
  }
}

run "admission_slots_are_wired" {
  command = plan

  assert {
    condition = (
      module.fn["dispatcher"].settings.environment.LIVE_SLOTS == "8"
      && module.fn["dispatcher"].settings.environment.BACKFILL_SLOTS == "2"
      && module.fn["dispatcher"].settings.environment.BACKFILL_QUEUE_ARN == module.backfill_queue.queue_arn
      && module.ingest_queue.settings.max_receive_count == 50
    )
    error_message = "The dispatcher needs its slot sizes and lanes; the queues tolerate records waiting for a slot."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.dispatcher.statement :
      s.sid == "AdmissionSlots" && tolist(one([for c in s.condition : c.values])) == tolist(["#SLOTS"])
    ])
    error_message = "The dispatcher may only touch the #SLOTS items of the status table."
  }

  assert {
    condition     = alltrue([for s in data.aws_iam_policy_document.mark_failed.statement : !contains(tolist(s.actions), "sns:Publish")])
    error_message = "MarkFailed no longer mails each failure (metric + hourly digest)."
  }
}

run "live_slots_follow_embed_concurrency" {
  command = plan

  variables {
    embed_reserved_concurrency = 4
  }

  assert {
    condition     = module.fn["dispatcher"].settings.environment.LIVE_SLOTS == "8" && module.fn["embed"].settings.reserved == 4
    error_message = "live_slots defaults to 2 x embed_reserved_concurrency."
  }
}

run "workflow_has_a_timeout_and_long_throttle_retries" {
  command = plan

  assert {
    condition     = jsondecode(local.ingest_definition).TimeoutSeconds == 10800
    error_message = "The ingest state machine needs a top-level timeout (runs-timing-out can fire)."
  }

  assert {
    condition = alltrue([
      for name, st in jsondecode(local.ingest_definition).States :
      try(st.Type, "") != "Task" || anytrue([
        for r in try(st.Retry, []) :
        contains(r.ErrorEquals, "Lambda.TooManyRequestsException") && contains(r.ErrorEquals, "IngestThrottled") && r.MaxAttempts == 12
      ])
    ])
    error_message = "Every task retries throttling with the long budget."
  }

  assert {
    condition = (
      jsondecode(local.ingest_definition).States.OcrStart.ResultSelector.polls == 0
      && jsondecode(local.ingest_definition).States.OcrPoll.Parameters.Payload["polls.$"] == "$.ocr.polls"
      && module.fn["ocr_poll"].settings.environment.OCR_MAX_POLLS == "180"
    )
    error_message = "The OCR poll loop counts its polls and gives up after OCR_MAX_POLLS."
  }
}

run "callers_invoke_the_live_aliases" {
  command = plan

  assert {
    condition = alltrue([
      for name in local.workflow_functions :
      strcontains(local.ingest_definition, module.fn[name].alias_arn)
    ])
    error_message = "The state machine invokes each step's live alias (rollback = move the alias)."
  }

  assert {
    condition = (
      module.reconcile_schedule.settings.target_arn == module.fn["reconciler"].alias_arn
      && module.control_api.settings.lambda_qualifier == "live"
    )
    error_message = "Schedules and the control API invoke the live aliases."
  }

  assert {
    condition = alltrue([
      for name in local.workflow_functions :
      contains(flatten([for s in data.aws_iam_policy_document.ingest_workflow.statement : tolist(s.resources)]), module.fn[name].alias_arn)
    ])
    error_message = "The state machine role may invoke the aliases."
  }
}

run "backups_are_prod_only_and_copied_to_another_region" {
  command = plan

  variables {
    env                = "prod"
    alarm_emails       = ["oncall@example.com"]
    backup_enabled     = true
    backup_copy_region = "us-east-2"
  }

  assert {
    condition = (
      length(module.backup[0].settings.resources) == 3
      && contains(module.backup[0].settings.resources, module.status_table.arn)
      && contains(module.backup[0].settings.resources, module.db[0].cluster_arn)
      && module.backup[0].settings.retention_days == 35
    )
    error_message = "prod backs up its database and tables daily for 35 days, with a copy in another region."
  }

  assert {
    condition     = alltrue([for b in local.role_permissions_boundaries : b == "arn:aws:iam::123456789012:policy/entingestion-prod-workload-boundary"]) && length(module.backup[0].role_permissions_boundaries) == 1
    error_message = "The backup role carries the workload boundary."
  }
}

run "no_backups_by_default" {
  command = plan

  assert {
    condition     = length(module.backup) == 0
    error_message = "AWS Backup is off unless backup_enabled (dev runs on the free plan)."
  }
}

# -- embedding profiles -----------------------------------------------------------------

run "embedding_profiles_reach_every_function" {
  command = plan

  assert {
    condition = (
      jsondecode(module.fn["embed"].settings.environment.EMBEDDING_PROFILES) == {
        primary = { provider = "bedrock", model = "amazon.titan-embed-text-v2:0", dimension = 1024, normalize = true, batch_size = 16, max_input_tokens = 8192 }
        compact = { provider = "bedrock", model = "amazon.titan-embed-text-v2:0", dimension = 512, normalize = true, batch_size = 16, max_input_tokens = 8192 }
      }
      && module.fn["embed"].settings.environment.DEFAULT_PROFILE == "primary"
      && module.fn["embed"].settings.environment.TENANT_PROFILES == "{}"
    )
    error_message = "The profiles reach the functions with their defaults filled in (numbers and booleans as JSON), chunks_primary live."
  }

  assert {
    condition = alltrue([
      for f in module.fn : !contains(keys(f.settings.environment), "TARGET_TABLE") && f.settings.environment.EMBEDDING_PROFILES == module.fn["embed"].settings.environment.EMBEDDING_PROFILES
    ])
    error_message = "Every function gets the same profiles, and no longer the single-model variables."
  }

  assert {
    condition = alltrue([
      for doc in [data.aws_iam_policy_document.embed, data.aws_iam_policy_document.control_api, data.aws_iam_policy_document.eval] :
      anytrue([
        for s in doc.statement : contains(tolist(s.actions), "bedrock:InvokeModel") && s.resources == toset(["arn:aws:bedrock:us-west-2::foundation-model/amazon.titan-embed-text-v2:0"])
      ])
      && alltrue([for s in doc.statement : !contains(tolist(s.actions), "secretsmanager:GetSecretValue")])
    ])
    error_message = "Embed, the control API and eval may invoke the Titan model, and read no API keys."
  }

  assert {
    condition     = module.alerts.settings["bedrock-throttled"].dimensions.ModelId == "amazon.titan-embed-text-v2:0"
    error_message = "The throttling alarm watches the default profile's model."
  }
}

run "embedding_profiles_for_cohere_and_an_openai_compatible_endpoint" {
  command = plan

  variables {
    embedding_profiles = {
      primary      = { provider = "bedrock", model = "amazon.titan-embed-text-v2:0", dimension = 1024 }
      multilingual = { provider = "bedrock", model = "cohere.embed-multilingual-v3", dimension = 1024, max_input_tokens = 512, batch_size = 96 }
      lite_768 = {
        provider       = "openai_compatible"
        model          = "nomic-embed-text"
        dimension      = 768
        base_url       = "https://litellm.example.com/v1"
        api_key_secret = "entingestion/dev/litellm"
      }
    }
    tenant_profiles = { "t-multi" = "multilingual" }
  }

  assert {
    condition = (
      jsondecode(module.fn["embed"].settings.environment.EMBEDDING_PROFILES).lite_768 == {
        provider       = "openai_compatible", model = "nomic-embed-text", dimension = 768, normalize = true,
        batch_size     = 16, max_input_tokens = 8192, base_url = "https://litellm.example.com/v1",
        api_key_secret = "entingestion/dev/litellm"
      }
      && jsondecode(module.fn["embed"].settings.environment.TENANT_PROFILES) == { "t-multi" = "multilingual" }
    )
    error_message = "Profiles and the tenant mapping reach the functions as JSON."
  }

  assert {
    condition = alltrue([
      for doc in [data.aws_iam_policy_document.embed, data.aws_iam_policy_document.control_api, data.aws_iam_policy_document.eval] :
      anytrue([
        for s in doc.statement : contains(tolist(s.actions), "bedrock:InvokeModel") && s.resources == toset([
          "arn:aws:bedrock:us-west-2::foundation-model/amazon.titan-embed-text-v2:0",
          "arn:aws:bedrock:us-west-2::foundation-model/cohere.embed-multilingual-v3",
        ])
      ])
      && anytrue([
        for s in doc.statement : s.sid == "EmbeddingApiKeys" && contains(tolist(s.actions), "secretsmanager:GetSecretValue") && length(s.actions) == 1 && s.resources == toset([
          "arn:aws:secretsmanager:us-west-2:123456789012:secret:entingestion/dev/litellm-??????",
        ])
      ])
    ])
    error_message = "Every function that embeds may invoke each Bedrock profile's model and read each endpoint's API key, nothing else."
  }
}

run "an_embedding_profile_needs_a_valid_name" {
  command = plan
  variables {
    embedding_profiles = { "Titan-1024" = { provider = "bedrock", model = "amazon.titan-embed-text-v2:0", dimension = 1024 } }
    default_profile    = "Titan-1024"
  }
  expect_failures = [var.embedding_profiles]
}

run "an_openai_compatible_profile_needs_an_https_base_url" {
  command = plan
  variables {
    embedding_profiles = {
      primary = { provider = "bedrock", model = "amazon.titan-embed-text-v2:0", dimension = 1024 }
      plain   = { provider = "openai_compatible", model = "m", dimension = 384, base_url = "http://emb.example.com" }
      missing = { provider = "openai_compatible", model = "m", dimension = 384 }
    }
  }
  expect_failures = [var.embedding_profiles]
}

run "an_embedding_dimension_must_fit_hnsw" {
  command = plan
  variables {
    embedding_profiles = {
      primary = { provider = "bedrock", model = "amazon.titan-embed-text-v2:0", dimension = 1024 }
      huge    = { provider = "openai_compatible", model = "m", dimension = 4096, base_url = "http://localhost:4000" }
    }
  }
  expect_failures = [var.embedding_profiles]
}

run "an_unknown_bedrock_model_family_is_refused" {
  command = plan
  variables {
    embedding_profiles = {
      primary = { provider = "bedrock", model = "amazon.titan-embed-text-v2:0", dimension = 1024 }
      image   = { provider = "bedrock", model = "amazon.titan-embed-image-v1", dimension = 1024 }
    }
  }
  expect_failures = [var.embedding_profiles]
}

run "the_default_and_tenant_profiles_must_exist" {
  command = plan
  variables {
    default_profile = "nope"
    tenant_profiles = { "t-a" = "missing" }
  }
  expect_failures = [var.default_profile, var.tenant_profiles]
}

run "embedding_profiles_must_fit_the_lambda_environment" {
  command = plan
  variables {
    embedding_profiles = {
      for i in range(12) : "profile_${i}" => {
        provider            = "openai_compatible"
        model               = "a-model-with-a-rather-long-name-${i}"
        dimension           = 768
        base_url            = "https://embeddings.internal.example.com/v1"
        api_key_secret      = "entingestion/dev/embedding-endpoint-key"
        document_input_type = "search_document"
      }
    }
    default_profile = "profile_0"
  }
  expect_failures = [var.embedding_profiles]
}

run "backups_need_a_copy_region" {
  command = plan

  variables {
    env                = "prod"
    alarm_emails       = ["oncall@example.com"]
    backup_enabled     = true
    backup_copy_region = null
  }

  expect_failures = [var.backup_copy_region]
}

run "a_profile_without_bedrock_has_no_throttle_alarm" {
  command = plan

  variables {
    embedding_profiles = {
      lite_768 = { provider = "openai_compatible", model = "nomic-embed-text", dimension = 768, base_url = "https://litellm.example.com/v1", api_key_secret = "entingestion/dev/litellm" }
    }
    default_profile = "lite_768"
  }

  assert {
    condition     = !contains(keys(module.alerts.settings), "bedrock-throttled")
    error_message = "Without a Bedrock profile there is no Bedrock throttling alarm (and no model to watch)."
  }

  assert {
    condition     = local.eval_tables == ["chunks_lite_768"]
    error_message = "The dashboard's quality panel shows one table per profile."
  }
}

run "the_throttle_alarm_falls_back_to_a_bedrock_profile" {
  command = plan

  variables {
    embedding_profiles = {
      lite_768     = { provider = "openai_compatible", model = "nomic-embed-text", dimension = 768, base_url = "https://litellm.example.com/v1", api_key_secret = "entingestion/dev/litellm" }
      multilingual = { provider = "bedrock", model = "cohere.embed-multilingual-v3", dimension = 1024, max_input_tokens = 512, batch_size = 96 }
    }
    default_profile = "lite_768"
  }

  assert {
    condition     = module.alerts.settings["bedrock-throttled"].dimensions.ModelId == "cohere.embed-multilingual-v3"
    error_message = "When the default profile isn't on Bedrock, the alarm watches the first Bedrock profile's model."
  }

  assert {
    condition     = toset(local.eval_tables) == toset(["chunks_lite_768", "chunks_multilingual"])
    error_message = "The dashboard's quality panel shows one table per profile."
  }
}

run "models_come_from_tfvars" {
  command = plan

  variables {
    rerank_enabled  = true
    rerank_model_id = "cohere.rerank-v3-5:0"
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.control_api.statement :
      s.sid == "RerankModel" && s.resources == toset(["arn:aws:bedrock:us-west-2::foundation-model/cohere.rerank-v3-5:0"])
    ])
    error_message = "The reranking model is whatever rerank_model_id names."
  }

  assert {
    condition     = local.eval_tables == ["chunks_compact", "chunks_primary"]
    error_message = "The dashboard's quality panel shows chunks_<profile> for every profile."
  }
}

run "callers_get_only_the_live_aliases" {
  command = plan

  assert {
    condition = (
      local.query_invoke_arns == ["arn:aws:lambda:us-west-2:123456789012:function:ingestion-dev-query:live"]
      && local.tables_invoke_arns == ["arn:aws:lambda:us-west-2:123456789012:function:ingestion-dev-tables:live"]
      && module.fn["eval"].settings.environment.QUERY_FUNCTION_NAME == "ingestion-dev-query:live"
      && module.fn["reconciler"].settings.environment.QUERY_FUNCTION_NAME == "ingestion-dev-query:live"
      && module.fn["control_api"].settings.environment.TABLES_FUNCTION_NAME == "ingestion-dev-tables:live"
    )
    error_message = "Every caller of query and tables names the live alias, and may invoke only it."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.eval.statement :
      s.sid == "GoldenSet" && s.resources == toset(["arn:aws:s3:::ingest-staging/_eval/golden/*"])
    ])
    error_message = "The eval function reads the versioned golden sets only."
  }
}

run "the_dispatcher_waits_for_a_slot_within_its_timeout" {
  command = plan

  assert {
    condition = (
      module.fn["dispatcher"].settings.environment.SLOT_WAIT_SECONDS == "40"
      && module.fn["dispatcher"].settings.timeout == 60
      && module.ingest_queue.settings.visibility_timeout_seconds == 360
      && module.backfill_queue.settings.visibility_timeout_seconds == 360
    )
    error_message = "The dispatcher waits up to slot_wait_seconds (40) inside its 60 s timeout; the queues' visibility is 6 x the timeout."
  }
}

run "the_dispatcher_timeout_is_configurable" {
  command = plan

  variables {
    dispatcher_timeout = 120
    slot_wait_seconds  = 90
  }

  assert {
    condition = (
      module.fn["dispatcher"].settings.timeout == 120
      && module.fn["dispatcher"].settings.environment.SLOT_WAIT_SECONDS == "90"
      && module.ingest_queue.settings.visibility_timeout_seconds == 720
    )
    error_message = "dispatcher_timeout and slot_wait_seconds reach the function and the queues."
  }
}

run "the_slot_wait_must_fit_the_dispatcher_timeout" {
  command = plan

  variables {
    slot_wait_seconds = 60
  }

  expect_failures = [var.slot_wait_seconds]
}

run "the_producer_hand_off_follows_the_environment" {
  command = plan

  variables {
    env           = "prod"
    alarm_emails  = ["oncall@example.com"]
    database_mode = "aurora"
  }

  assert {
    condition = (
      local.producer_ssm_prefix == "/entingestion/prod/producer"
      && data.aws_ssm_parameter.producer["articles_stream_arn"].name == "/entingestion/prod/producer/articles_stream_arn"
      && module.fn["control_api"].settings.environment.SOURCE_ACCESS_PARAMETER == "/entingestion/prod/producer/source_access"
    )
    error_message = "The producer's hand-off is read from <ssm_prefix>/<env>/producer."
  }

  assert {
    condition     = output.ssm_parameter_names == { control_api_url = "/entingestion/prod/ingestion/control_api_url", control_api_execution_arn = "/entingestion/prod/ingestion/control_api_execution_arn" }
    error_message = "ssm_parameter_names lists what the stack publishes for the ui stack."
  }
}

run "the_producer_hand_off_can_be_overridden" {
  command = plan

  variables {
    producer_ssm_prefix = "/shared/dev/producer"
  }

  assert {
    condition     = module.fn["control_api"].settings.environment.SOURCE_ACCESS_PARAMETER == "/shared/dev/producer/source_access"
    error_message = "producer_ssm_prefix overrides the derived prefix."
  }
}
