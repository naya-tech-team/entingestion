data "aws_partition" "current" {}

locals {
  is_image = var.code.image_uri != null
}

# Created before the function, so Lambda never auto-creates an unencrypted, never-expiring group.
resource "aws_cloudwatch_log_group" "this" {
  #checkov:skip=CKV_AWS_338:Retention is set per environment (brief §7.3: 30 days dev, 90 days prod).
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name                 = var.name
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  permissions_boundary = var.permissions_boundary
}

data "aws_iam_policy_document" "runtime" {
  # Scoped to this function's log group instead of AWSLambdaBasicExecutionRole (logs:* on all groups).
  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }

  statement {
    #checkov:skip=CKV_AWS_356:X-Ray trace APIs don't support resource-level permissions.
    sid       = "XRay"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "runtime" {
  name   = "runtime"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.runtime.json
}

# ENI management for functions in a VPC (the AWS-managed policy is scoped by Lambda itself).
resource "aws_iam_role_policy_attachment" "vpc_access" {
  count      = var.vpc_config == null ? 0 : 1
  role       = aws_iam_role.this.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "function" {
  name   = "function"
  role   = aws_iam_role.this.id
  policy = var.policy_json
}

resource "aws_lambda_function" "this" {
  #checkov:skip=CKV_AWS_116:Callers handle failures (API: synchronous; SQS-driven: the queue's own DLQ).
  #checkov:skip=CKV_AWS_117:Uses only public AWS endpoints; no VPC resources to reach.
  #checkov:skip=CKV_AWS_272:Code signing isn't used; the zip is built and deployed by this pipeline.
  #checkov:skip=CKV_AWS_115:Reserved concurrency is opt-in via reserved_concurrent_executions.
  #checkov:skip=CKV_AWS_50:Tracing defaults to Active; PassThrough is an explicit opt-out for VPC functions without an X-Ray endpoint.
  function_name = var.name
  description   = var.description
  role          = aws_iam_role.this.arn
  package_type  = local.is_image ? "Image" : "Zip"
  handler       = local.is_image ? null : var.handler
  runtime       = local.is_image ? null : var.runtime
  architectures = [var.architecture]
  layers        = local.is_image ? null : (length(var.layers) > 0 ? var.layers : null)
  memory_size   = var.memory_size
  timeout       = var.timeout
  kms_key_arn   = var.kms_key_arn

  filename         = var.code.zip_path
  s3_bucket        = var.code.s3_bucket
  s3_key           = var.code.s3_key
  image_uri        = var.code.image_uri
  source_code_hash = var.code.source_code_hash

  # One image can serve several functions; the handler picks the entry point.
  dynamic "image_config" {
    for_each = local.is_image ? [var.handler] : []

    content {
      command = [image_config.value]
    }
  }

  dynamic "vpc_config" {
    for_each = var.vpc_config == null ? [] : [var.vpc_config]

    content {
      subnet_ids         = vpc_config.value.subnet_ids
      security_group_ids = vpc_config.value.security_group_ids
    }
  }

  reserved_concurrent_executions = var.reserved_concurrent_executions

  # Every code or configuration change publishes a version; callers use the alias below, so a
  # bad deploy rolls back by pointing the alias at the previous version (make rollback).
  publish = true

  dynamic "environment" {
    for_each = length(var.environment) > 0 ? [var.environment] : []

    content {
      variables = environment.value
    }
  }

  tracing_config {
    mode = var.tracing_mode
  }

  depends_on = [
    aws_cloudwatch_log_group.this,
    aws_iam_role_policy.runtime,
    aws_iam_role_policy.function,
    aws_iam_role_policy_attachment.vpc_access,
  ]
}

# What every caller invokes (state machines, queues, schedules, APIs). Each apply moves it to the
# version that apply published; `make rollback` moves it back without touching the code.
resource "aws_lambda_alias" "live" {
  name             = var.alias_name
  description      = "The version callers use; moved by each deploy and by make rollback."
  function_name    = aws_lambda_function.this.function_name
  function_version = aws_lambda_function.this.version
}
