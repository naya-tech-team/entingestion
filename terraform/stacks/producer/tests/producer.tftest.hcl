# Plan-time assertions for the producer stack.
# Guarantees the generic modules always enforce (bucket public access block and TLS-only policy,
# table PITR) are asserted in each module's own tests under terraform/modules/*/tests.
mock_provider "aws" {
  # Make mocked computed values (ARNs) known at plan time so assertions can compare them.
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
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_data "aws_ssm_parameters_by_path" {
    # Before the ui stack exists: nothing under <ssm_prefix>/<env>/ui.
    defaults = { names = [], values = [] }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::producer-test-raw" }
  }
  mock_resource "aws_dynamodb_table" {
    defaults = { arn = "arn:aws:dynamodb:us-west-2:123456789012:table/mock" }
  }
  mock_resource "aws_api_gateway_rest_api" {
    defaults = {
      id               = "abc123"
      root_resource_id = "root01"
      execution_arn    = "arn:aws:execute-api:us-west-2:123456789012:abc123"
    }
  }
  mock_resource "aws_sqs_queue" {
    defaults = { arn = "arn:aws:sqs:us-west-2:123456789012:mock" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:us-west-2:123456789012:function:mock", version = "1" }
  }
  mock_resource "aws_lambda_alias" {
    defaults = {
      arn        = "arn:aws:lambda:us-west-2:123456789012:function:mock:live"
      invoke_arn = "arn:aws:apigateway:us-west-2:lambda:path/2015-03-31/functions/arn:aws:lambda:us-west-2:123456789012:function:mock:live/invocations"
    }
  }
  mock_resource "aws_api_gateway_stage" {
    defaults = { arn = "arn:aws:apigateway:us-west-2::/restapis/abc123/stages/prod" }
  }
  mock_resource "aws_wafv2_web_acl" {
    defaults = { arn = "arn:aws:wafv2:us-west-2:123456789012:regional/webacl/producer-dev/abc" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-west-2:123456789012:producer-dev-alarms" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-west-2:123456789012:log-group:mock" }
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

  backup_copy_region = null

  # S3 artifact mode, so tests don't need a built zip.
  lambda_s3_bucket           = "artifacts"
  lambda_s3_key              = "producer/test.zip"
  lambda_s3_source_code_hash = "abc="
}

run "articles_stream_is_new_and_old_images" {
  command = plan

  assert {
    condition = (
      module.articles_table.settings.stream_enabled
      && module.articles_table.settings.stream_view_type == "NEW_AND_OLD_IMAGES"
    )
    error_message = "articles must stream NEW_AND_OLD_IMAGES."
  }
}

run "table_names_and_keys_match_the_contract" {
  command = plan

  variables {
    table_prefix = "dev-"
  }

  assert {
    condition = (
      module.process_table.settings.name == "dev-process"
      && module.articles_table.settings.name == "dev-articles"
      && module.uploads_table.settings.name == "dev-producer_uploads"
    )
    error_message = "Table names must be <table_prefix>process|articles|producer_uploads."
  }

  assert {
    condition = (
      module.process_table.settings.hash_key == "process_id"
      && module.articles_table.settings.hash_key == "article_id"
      && module.uploads_table.settings.hash_key == "upload_id"
    )
    error_message = "Partition keys are the contract with the ingestion pipeline."
  }

  assert {
    condition = (
      [for g in module.articles_table.settings.global_secondary_index : "${g.name}:${g.hash_key}:${g.range_key}:${g.projection_type}"] == ["gsi_updated_shard:updated_shard:updated_at:INCLUDE"]
    )
    error_message = "articles has one index, gsi_updated_shard (INCLUDE), for the ingestion reconciler."
  }

  assert {
    condition     = module.uploads_table.settings.ttl_attribute == "expires_at_epoch"
    error_message = "producer_uploads TTL must be on expires_at_epoch."
  }
}

run "all_tables_have_pitr_and_kms" {
  command = plan

  assert {
    condition = alltrue([
      for t in [
        module.process_table.settings,
        module.articles_table.settings,
        module.uploads_table.settings,
      ] :
      t.point_in_time_recovery
      && t.sse_enabled
      && t.sse_kms_key_arn == module.kms.key_arn
    ])
    error_message = "Every table must have PITR and SSE with the producer CMK."
  }
}

run "raw_bucket_is_versioned_with_lifecycle" {
  command = plan

  assert {
    condition     = module.raw_bucket.settings.versioning_status == "Enabled"
    error_message = "Raw bucket must be versioned."
  }

  assert {
    condition = (
      module.raw_bucket.settings.lifecycle_rules[0].abort_incomplete_multipart_upload_days == 7
      && module.raw_bucket.settings.lifecycle_rules[0].noncurrent_version_transitions[0].days == 30
      && module.raw_bucket.settings.lifecycle_rules[0].noncurrent_version_transitions[0].storage_class == "GLACIER_IR"
    )
    error_message = "Raw bucket lifecycle must abort MPUs after 7 days and move noncurrent versions to GLACIER_IR after 30."
  }
}

run "cors_only_when_origins_given" {
  command = plan

  assert {
    condition     = length(module.raw_bucket.settings.cors_rules) == 0
    error_message = "No CORS without origins."
  }
}

run "cors_allows_ticket_headers" {
  command = plan

  variables {
    ui_extra_origins = ["https://app.example.com"]
  }

  assert {
    condition = length(setsubtract(
      ["content-type", "x-amz-checksum-sha256", "x-amz-meta-upload-id"],
      module.raw_bucket.settings.cors_rules[0].allowed_headers,
    )) == 0
    error_message = "CORS must allow every header the upload ticket tells the client to send."
  }
}

run "cors_allows_the_published_ui_origin" {
  command = plan

  override_data {
    target = data.aws_ssm_parameters_by_path.ui
    values = {
      names  = ["/entingestion/dev/ui/origin", "/entingestion/dev/ui/other"]
      values = ["https://d111111abcdef8.cloudfront.net", "not-an-origin"]
    }
  }

  variables {
    ui_extra_origins = ["http://localhost:5173", "https://d111111abcdef8.cloudfront.net"]
  }

  assert {
    condition = (
      length(module.raw_bucket.settings.cors_rules) == 1
      && module.raw_bucket.settings.cors_rules[0].allowed_methods == tolist(["PUT"])
      && module.raw_bucket.settings.cors_rules[0].allowed_origins == tolist(["https://d111111abcdef8.cloudfront.net", "http://localhost:5173"])
      && contains(module.raw_bucket.settings.cors_rules[0].expose_headers, "ETag")
    )
    error_message = "The UI's published origin (and only .../ui/origin), plus the extra origins, may PUT to the raw bucket."
  }
}

run "ui_extra_origins_must_be_https_or_localhost" {
  command = plan

  variables {
    ui_extra_origins = ["http://app.example.com"]
  }

  expect_failures = [var.ui_extra_origins]
}

run "dev_is_disposable" {
  command = plan

  assert {
    condition = (
      module.raw_bucket.settings.force_destroy
      && !module.articles_table.settings.deletion_protection
    )
    error_message = "dev bucket must be force-destroyable and tables unprotected."
  }
}

run "prod_has_deletion_protection" {
  command = plan

  variables {
    env           = "prod"
    alarm_emails  = ["oncall@example.com"]
    source_access = { news = { tenant_id = "news", writers = ["arn:aws:iam::123456789012:role/news"] } }
  }

  assert {
    condition = alltrue([
      module.process_table.settings.deletion_protection,
      module.articles_table.settings.deletion_protection,
      module.uploads_table.settings.deletion_protection,
    ])
    error_message = "prod tables must have deletion protection."
  }

  assert {
    condition     = !module.raw_bucket.settings.force_destroy
    error_message = "prod raw bucket must not be force-destroyable."
  }
}

run "publishes_ssm_parameters" {
  command = plan

  assert {
    condition = (
      aws_ssm_parameter.published["articles_stream_arn"].name == "/entingestion/dev/producer/articles_stream_arn"
      && length(aws_ssm_parameter.published) == 8
      && aws_ssm_parameter.published["process_table_name"].name == "/entingestion/dev/producer/process_table_name"
    )
    error_message = "Producer must publish its hand-off values under /entingestion/<env>/producer/."
  }

  assert {
    condition = (
      aws_ssm_parameter.published["api_url"].name == "/entingestion/dev/producer/api_url"
      && aws_ssm_parameter.published["api_execution_arn"].value == "arn:aws:execute-api:us-west-2:123456789012:abc123"
    )
    error_message = "The upload UI needs the API's invoke URL and execution ARN."
  }
}

run "api_uses_iam_auth_on_both_methods" {
  command = plan

  assert {
    condition = (
      module.api.settings.root_authorization == "AWS_IAM"
      && module.api.settings.proxy_authorization == "AWS_IAM"
      && module.api.settings.integration_type == "AWS_PROXY"
    )
    error_message = "Both API methods must use AWS_IAM with a Lambda proxy integration."
  }

  assert {
    condition     = module.api.settings.endpoint_types == tolist(["REGIONAL"]) && module.api.settings.xray_tracing_enabled
    error_message = "API must be REGIONAL with X-Ray on the stage."
  }
}

run "api_function_matches_the_contract" {
  command = plan

  assert {
    condition = (
      module.api_function.settings.handler == "producer.api.lambda_handler"
      && module.api_function.settings.memory_size == 512
      && module.api_function.settings.timeout == 29
      && module.api_function.settings.architectures == tolist(["arm64"])
      && module.api_function.settings.runtime == "python3.12"
      && module.api_function.settings.tracing_mode == "Active"
    )
    error_message = "api function must be producer.api.lambda_handler, 512 MB, 29 s, arm64, python3.12, tracing Active."
  }

  assert {
    condition = alltrue([
      for k in ["PROCESS_TABLE", "ARTICLES_TABLE", "UPLOADS_TABLE", "RAW_BUCKET", "RAW_PREFIX",
        "POWERTOOLS_SERVICE_NAME", "POWERTOOLS_METRICS_NAMESPACE", "POWERTOOLS_LOG_LEVEL",
      "MAX_FILE_BYTES", "UPLOAD_URL_TTL_SECONDS"] :
      contains(keys(module.api_function.settings.environment), k)
    ])
    error_message = "api function is missing required environment variables."
  }

  assert {
    condition     = module.api_function.settings.environment.MAX_FILE_BYTES == "5368709120"
    error_message = "MAX_FILE_BYTES must default to 5 GiB."
  }
}

run "api_role_cannot_delete_from_process_or_uploads" {
  command = plan

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.api.statement :
      !contains(s.actions, "dynamodb:DeleteItem") if !contains(["ArticlesTable", "IdempotencyRecords"], s.sid)
    ])
    error_message = "The API role may delete only articles and its own idempotency records (a failed request's record is removed so it can be retried)."
  }

  assert {
    condition = alltrue(flatten([
      for s in data.aws_iam_policy_document.api.statement : [
        for r in s.resources : r != "*"
      ]
    ]))
    error_message = "API role policy must not use resource wildcards."
  }
}

run "zip_mode_hashes_the_local_package" {
  command = plan

  variables {
    lambda_s3_bucket           = null
    lambda_s3_key              = null
    lambda_s3_source_code_hash = null
    lambda_zip_path            = "tests/fixtures/lambda.zip"
  }

  assert {
    condition     = module.lambda_artifact.code.zip_path == "tests/fixtures/lambda.zip" && module.lambda_artifact.code.source_code_hash == filebase64sha256("tests/fixtures/lambda.zip")
    error_message = "Zip mode must deploy the local zip with its filebase64sha256."
  }
}

run "registrar_wiring_matches_the_contract" {
  command = plan

  assert {
    condition = (
      module.registrar_trigger.settings.batch_size == 10
      && module.registrar_trigger.settings.batching_window_seconds == 5
      && contains(module.registrar_trigger.settings.function_response_types, "ReportBatchItemFailures")
    )
    error_message = "Event source mapping must use batch 10, a 5 s window and ReportBatchItemFailures."
  }

  assert {
    condition = (
      module.registrar_queue.settings.visibility_timeout_seconds == 1800
      && module.registrar_queue.settings.max_receive_count == 5
      && module.registrar_queue.settings.dlq_message_retention_seconds == 1209600
      && module.registrar_queue.settings.sse_enabled
    )
    error_message = "Queue must have 1800 s visibility (6x timeout), DLQ after 5 receives, 14-day DLQ, SSE."
  }

  assert {
    condition = (
      module.registrar_function.settings.handler == "producer.registrar.lambda_handler"
      && module.registrar_function.settings.memory_size == 1024
      && module.registrar_function.settings.timeout == 300
      && module.registrar_function.settings.tracing_mode == "Active"
    )
    error_message = "registrar must be producer.registrar.lambda_handler, 1024 MB, 300 s, tracing Active."
  }

  assert {
    condition = (
      one(aws_s3_bucket_notification.raw.queue).filter_prefix == "raw/"
      && one(aws_s3_bucket_notification.raw.queue).events == toset(["s3:ObjectCreated:*"])
    )
    error_message = "Bucket must notify the queue of ObjectCreated under raw/ only."
  }
}

run "registrar_queue_accepts_only_this_bucket" {
  command = plan

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.registrar_queue.statement :
      contains([for c in s.condition : c.variable], "aws:SourceArn")
      && contains([for c in s.condition : c.variable], "aws:SourceAccount")
    ])
    error_message = "Queue policy must restrict S3 by aws:SourceArn and aws:SourceAccount."
  }
}

run "registrar_role_has_no_deletes_or_wildcards" {
  command = plan

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.registrar.statement :
      !contains(s.actions, "dynamodb:DeleteItem") && !contains(s.actions, "s3:PutObject")
    ])
    error_message = "Registrar must not delete items or write objects."
  }

  assert {
    condition = alltrue(flatten([
      for s in data.aws_iam_policy_document.registrar.statement : [for r in s.resources : r != "*"]
    ]))
    error_message = "Registrar role policy must not use resource wildcards."
  }
}

run "waf_protects_the_api_by_default" {
  command = plan

  assert {
    condition     = length(module.waf) == 1 && contains(module.waf[0].settings.associated, "api")
    error_message = "The stack must create a WAF web ACL and associate it with the API stage."
  }

  assert {
    condition     = contains(module.waf[0].settings.count_rules["AWSManagedRulesCommonRuleSet"], "SizeRestrictions_BODY")
    error_message = "SizeRestrictions_BODY must only count (bodies may exceed 8 KB)."
  }

  assert {
    condition     = length(aws_wafv2_web_acl_association.external) == 0
    error_message = "No external association when the stack creates the ACL."
  }
}

run "bring_your_own_waf" {
  command = plan

  variables {
    waf_acl_arn = "arn:aws:wafv2:us-west-2:123456789012:regional/webacl/shared/xyz"
  }

  assert {
    condition = (
      length(module.waf) == 0
      && aws_wafv2_web_acl_association.external[0].web_acl_arn == var.waf_acl_arn
      && output.web_acl_arn == var.waf_acl_arn
    )
    error_message = "waf_acl_arn must replace the created ACL with an association to it."
  }
}

run "alarms_cover_the_upload_path" {
  command = plan

  assert {
    condition = (
      module.alarms.settings["registrar-dlq-new-messages"].threshold == 1
      && module.alarms.settings["registrar-dlq-new-messages"].metrics.visible.metric_name == "ApproximateNumberOfMessagesVisible"
      && module.alarms.settings["registrar-queue-backlog"].metric_name == "ApproximateAgeOfOldestMessage"
    )
    error_message = "The DLQ alarms on each new message (redrive moves aren't counted as sent); the queue on its oldest message's age."
  }

  assert {
    condition = (
      module.alarms.settings["registrar-errors"].evaluation_periods == 3
      && module.alarms.settings["registrar-errors"].datapoints_to_alarm == 3
      && module.alarms.settings["registrar-errors"].period == 300
    )
    error_message = "Registrar errors alarm must require 3 x 5 min."
  }

  assert {
    condition = (
      module.alarms.settings["api-5xx"].threshold == 5
      && module.alarms.settings["api-5xx"].dimensions.Stage == "prod"
    )
    error_message = "API 5XX alarm must fire at 5 in 5 minutes on the prod stage."
  }

  assert {
    condition = (
      module.alarms.settings["uploads-rejected"].namespace == "Producer"
      && module.alarms.settings["uploads-rejected"].metric_name == "Upload_rejected"
      && module.alarms.settings["uploads-rejected"].treat_missing_data == "notBreaching"
    )
    error_message = "Rejected-upload alarm must watch Producer/Upload_rejected, missing data not breaching."
  }
}

run "principals_relative_to_the_account_get_its_arn" {
  command = plan

  variables {
    admin_principals = ["role/entingestion-dev-deploy"]
    source_access = {
      news = {
        tenant_id = "t-news"
        writers   = ["role/news-*", "arn:aws:iam::210987654321:role/partner"]
        readers   = ["user/analyst"]
      }
    }
  }

  assert {
    condition = (
      jsondecode(local.function_environment["SOURCE_ACCESS"]).admins == ["arn:aws:iam::123456789012:role/entingestion-dev-deploy"]
      && jsondecode(local.function_environment["SOURCE_ACCESS"]).sources.news.writers == ["arn:aws:iam::123456789012:role/news-*", "arn:aws:iam::210987654321:role/partner"]
      && jsondecode(local.function_environment["SOURCE_ACCESS"]).sources.news.readers == ["arn:aws:iam::123456789012:user/analyst"]
    )
    error_message = "Relative principals get this account's ARN (aws_account_id); full ARNs are kept."
  }
}

run "operators_from_the_environment_write_every_source" {
  command = plan

  variables {
    operator_principals = ["user/operator"]
    source_access = {
      news   = { tenant_id = "t-news", writers = ["role/news"] }
      sports = { tenant_id = "t-sports", writers = ["user/operator"] }
    }
  }

  assert {
    condition = (
      jsondecode(local.function_environment["SOURCE_ACCESS"]).sources.news.writers == ["arn:aws:iam::123456789012:role/news", "arn:aws:iam::123456789012:user/operator"]
      && jsondecode(local.function_environment["SOURCE_ACCESS"]).sources.sports.writers == ["arn:aws:iam::123456789012:user/operator"]
    )
    error_message = "operator_principals are added (once) to every source's writers."
  }
}

run "principals_must_be_relative_or_full_arns" {
  command = plan

  variables {
    admin_principals = ["entingestion-dev-deploy"]
    source_access    = { news = { tenant_id = "news", writers = ["some-user"] } }
  }

  expect_failures = [var.admin_principals, var.source_access]
}

run "sources_are_authorized_by_the_access_policy" {
  command = plan

  variables {
    admin_principals = ["arn:aws:iam::123456789012:role/entingestion-dev-deploy"]
    source_access = {
      news = { tenant_id = "t-news", writers = ["arn:aws:iam::123456789012:role/news-*"] }
    }
  }

  assert {
    condition = (
      jsondecode(local.function_environment["SOURCE_ACCESS"]).sources.news.tenant_id == "t-news"
      && jsondecode(local.function_environment["SOURCE_ACCESS"]).admins[0] == "arn:aws:iam::123456789012:role/entingestion-dev-deploy"
      && aws_ssm_parameter.published["source_access"].value == local.function_environment["SOURCE_ACCESS"]
    )
    error_message = "The api and registrar functions get the access policy, and ingestion can read it from SSM."
  }

  assert {
    condition     = one(data.aws_iam_policy_document.invoke_reader.statement).resources == toset(["arn:aws:execute-api:us-west-2:123456789012:abc123/prod/GET/v1/*"])
    error_message = "The reader policy allows GET routes only."
  }

  assert {
    condition     = jsondecode(local.function_environment["SOURCE_ACCESS"]).sources.news.pii == "allow"
    error_message = "A source's PII policy defaults to allow (ingestion applies it when pii_screening is on)."
  }
}

run "a_pii_policy_must_be_known" {
  command = plan

  variables {
    source_access = {
      news = { tenant_id = "t-news", writers = [], pii = "hide" }
    }
  }

  expect_failures = [var.source_access]
}

run "prod_needs_an_access_policy" {
  command = plan

  variables {
    env           = "prod"
    alarm_emails  = ["oncall@example.com"]
    source_access = {}
  }

  expect_failures = [var.source_access]
}

run "uploads_settle_and_unused_objects_expire" {
  command = plan

  assert {
    condition = (
      contains([for g in module.uploads_table.settings.global_secondary_index : g.name], "gsi_member")
      && contains([for g in module.uploads_table.settings.global_secondary_index : g.name], "gsi_pending")
      && contains([for g in module.articles_table.settings.global_secondary_index : g.name], "gsi_updated_shard")
    )
    error_message = "Uploads are indexed by process membership and by pending ticket; articles by a sharded update day."
  }

  assert {
    condition = anytrue([
      for g in module.uploads_table.settings.global_secondary_index :
      g.name == "gsi_process_uploads" && g.hash_key == "process_id" && g.range_key == "created_at"
      && g.projection_type == "INCLUDE"
      && length(setsubtract(["article_id", "filename", "status", "reason", "finished_at", "kind"], g.non_key_attributes)) == 0
    ])
    error_message = "GET /v1/processes/{id}/uploads reads gsi_process_uploads, which must carry the listed fields and `kind` (membership markers are filtered by it)."
  }

  assert {
    condition = anytrue([
      for g in module.uploads_table.settings.global_secondary_index :
      g.name == "gsi_pending" && toset(g.non_key_attributes) == toset(["status", "s3_key", "process_id"])
    ])
    error_message = "gsi_pending projects what the janitor reads: status, s3_key and process_id."
  }

  assert {
    condition = (
      length([for r in module.raw_bucket.settings.lifecycle_rules : r if lookup(r.tags, "producer-unused", "") == "rejected"]) == 1
      && length([for r in module.raw_bucket.settings.lifecycle_rules : r if lookup(r.tags, "producer-unused", "") == "superseded"]) == 1
      && one([for r in module.raw_bucket.settings.lifecycle_rules : r.noncurrent_version_expiration_days if r.noncurrent_version_expiration_days != null]) == 365
    )
    error_message = "Rejected and superseded uploads expire, and old versions are eventually deleted."
  }

  assert {
    condition = (
      module.janitor_schedule.settings.schedule_expression == "rate(1 hour)"
      && module.janitor_schedule.settings.target_arn == module.janitor_function.alias_arn
    )
    error_message = "The janitor runs hourly."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.registrar.statement :
      s.sid == "UploadsTable" && contains(s.actions, "dynamodb:ConditionCheckItem")
    ])
    error_message = "The registrar checks the delete tombstone in its article transaction."
  }
}

run "raw_uploads_only_by_ticket" {
  command = plan

  assert {
    condition = anytrue([
      for st in data.aws_iam_policy_document.raw_uploads_only_by_ticket.statement :
      st.sid == "OnlyUploadTicketsWriteRaw" && st.effect == "Deny" && one(st.condition).test == "StringNotEquals" && one(st.condition).variable == "aws:PrincipalArn" && length(one(st.condition).values) == 1
    ])
    error_message = "Only the API role (which signs upload tickets) may put objects under raw/."
  }

  assert {
    condition     = local.function_environment["IDEMPOTENCY_TABLE"] == module.idempotency_table.name && module.idempotency_table.settings.ttl_attribute == "expiration"
    error_message = "Idempotency records live in their own table and expire."
  }
}

run "every_role_carries_the_workload_boundary" {
  command = plan

  assert {
    condition = (
      length(local.role_permissions_boundaries) == 4 # api, registrar, janitor and its schedule
      && alltrue([for b in local.role_permissions_boundaries : b == "arn:aws:iam::123456789012:policy/entingestion-dev-workload-boundary"])
    )
    error_message = "Both function roles must carry entingestion-<env>-workload-boundary (the deploy role can't create them otherwise)."
  }
}

run "workload_boundary_can_be_turned_off" {
  command = plan

  variables {
    workload_boundary_enabled = false
  }

  assert {
    condition     = alltrue([for b in local.role_permissions_boundaries : b == null])
    error_message = "workload_boundary_enabled = false leaves the roles unbounded."
  }
}

# -- M5 operations ------------------------------------------------------------------------

run "prod_refuses_to_plan_without_an_alert_subscriber" {
  command = plan

  variables {
    env           = "prod"
    alarm_emails  = []
    source_access = { news = { tenant_id = "news", writers = ["arn:aws:iam::123456789012:role/news"] } }
  }

  expect_failures = [var.alarm_emails]
}

run "callers_invoke_the_live_aliases" {
  command = plan

  assert {
    condition = (
      module.registrar_trigger.settings.function_name == module.registrar_function.alias_arn
      && module.api.settings.lambda_qualifier == "live"
      && module.registrar_function.settings.publish
    )
    error_message = "The registrar mapping, the API and the janitor schedule invoke the live aliases."
  }

  assert {
    condition     = module.registrar_function.settings.reserved == -1
    error_message = "The registrar is unreserved unless registrar_reserved_concurrency is set."
  }
}

run "registrar_concurrency_can_be_reserved" {
  command = plan

  variables {
    registrar_reserved_concurrency = 10
  }

  assert {
    condition     = module.registrar_function.settings.reserved == 10
    error_message = "registrar_reserved_concurrency must reach the function."
  }
}

run "backups_are_prod_only_and_copied_to_another_region" {
  command = plan

  variables {
    env                = "prod"
    alarm_emails       = ["oncall@example.com"]
    source_access      = { news = { tenant_id = "news", writers = ["arn:aws:iam::123456789012:role/news"] } }
    backup_enabled     = true
    backup_copy_region = "us-east-2"
  }

  assert {
    condition = (
      length(module.backup[0].settings.resources) == 3
      && contains(module.backup[0].settings.resources, module.articles_table.arn)
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

run "backups_need_a_copy_region" {
  command = plan

  variables {
    env                = "prod"
    alarm_emails       = ["oncall@example.com"]
    source_access      = { news = { tenant_id = "news", writers = ["arn:aws:iam::123456789012:role/news"] } }
    backup_enabled     = true
    backup_copy_region = null
  }

  expect_failures = [var.backup_copy_region]
}

# -- shared derivations (modules/stack_common): unchanged by the module --------------------------

run "default_tags_are_the_mandatory_tags_and_env" {
  command = plan

  variables {
    tags = { cost_center = "42" }
  }

  assert {
    condition = module.common.default_tags == {
      "Name"                 = "producer-dev"
      "Created By"           = "test"
      "Created Date"         = "2026-01-01"
      "Owner"                = "test"
      "Internal Client Code" = "test"
      "Client"               = "test"
      "Project Name"         = "test"
      "Division"             = "test"
      "env"                  = "dev"
      "cost_center"          = "42"
    }
    error_message = "The provider tags every resource with the extra tags, the 8 mandatory tags (Name = producer-<env>) and env."
  }

  assert {
    condition     = local.name_prefix == "producer-dev" && module.api_function.function_name == "producer-dev-api"
    error_message = "Names are producer-<env>-*."
  }
}

run "boundary_and_access_policy_json_are_unchanged" {
  command = plan

  variables {
    operator_principals = ["user/operator"]
    admin_principals    = ["role/entingestion-dev-deploy"]
    source_access = {
      news = { tenant_id = "t-news", writers = ["role/news", "user/operator"], readers = ["arn:aws:iam::210987654321:role/search"], pii = "redact" }
    }
  }

  assert {
    condition     = local.permissions_boundary == "arn:aws:iam::123456789012:policy/entingestion-dev-workload-boundary" && module.boundary_check.all_bounded
    error_message = "The workload boundary ARN is unchanged and every role carries it."
  }

  assert {
    condition     = local.source_access_json == "{\"admins\":[\"arn:aws:iam::123456789012:role/entingestion-dev-deploy\"],\"sources\":{\"news\":{\"pii\":\"redact\",\"readers\":[\"arn:aws:iam::210987654321:role/search\"],\"tenant_id\":\"t-news\",\"writers\":[\"arn:aws:iam::123456789012:role/news\",\"arn:aws:iam::123456789012:user/operator\"]}}}"
    error_message = "SOURCE_ACCESS (and its SSM copy) keeps its exact JSON."
  }
}
