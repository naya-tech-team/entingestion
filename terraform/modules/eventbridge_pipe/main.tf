data "aws_caller_identity" "current" {}

resource "aws_cloudwatch_log_group" "this" {
  #checkov:skip=CKV_AWS_338:Retention is set by the caller per environment.
  name              = "/aws/vendedlogs/pipes/${var.name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["pipes.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "this" {
  name                 = "${var.name}-pipe"
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  permissions_boundary = var.permissions_boundary
}

# Least privilege derived from the source and target ARNs.
data "aws_iam_policy_document" "this" {
  source_policy_documents = var.policy_documents

  statement {
    sid       = "ReadSourceStream"
    actions   = ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator"]
    resources = [var.source_stream_arn]
  }

  statement {
    sid       = "SendToTargetAndDlq"
    actions   = ["sqs:SendMessage"]
    resources = [var.target_queue_arn, var.dlq_arn]
  }

  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }
}

resource "aws_iam_role_policy" "this" {
  name   = "pipe"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.this.json
}

resource "aws_pipes_pipe" "this" {
  name          = var.name
  role_arn      = aws_iam_role.this.arn
  source        = var.source_stream_arn
  target        = var.target_queue_arn
  desired_state = "RUNNING"

  source_parameters {
    dynamodb_stream_parameters {
      starting_position                  = var.starting_position
      batch_size                         = var.batch_size
      maximum_batching_window_in_seconds = var.batching_window_seconds
      maximum_retry_attempts             = var.max_retries
      maximum_record_age_in_seconds      = var.max_record_age_seconds
      on_partial_batch_item_failure      = "AUTOMATIC_BISECT"

      dead_letter_config {
        arn = var.dlq_arn
      }
    }

    dynamic "filter_criteria" {
      for_each = length(var.filter_patterns) > 0 ? [var.filter_patterns] : []

      content {
        dynamic "filter" {
          for_each = filter_criteria.value

          content {
            pattern = filter.value
          }
        }
      }
    }
  }

  dynamic "target_parameters" {
    for_each = var.input_template == null ? [] : [var.input_template]

    content {
      input_template = target_parameters.value
    }
  }

  log_configuration {
    level = var.log_level

    cloudwatch_logs_log_destination {
      log_group_arn = aws_cloudwatch_log_group.this.arn
    }
  }

  depends_on = [aws_iam_role_policy.this]
}
