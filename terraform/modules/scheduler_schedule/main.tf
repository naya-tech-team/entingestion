data "aws_caller_identity" "current" {}

# The role EventBridge Scheduler assumes to call the target. Only this account's schedules can
# assume it (confused-deputy guard).
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "this" {
  name                 = "${var.name}-scheduler"
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  permissions_boundary = var.permissions_boundary
}

data "aws_iam_policy_document" "invoke" {
  statement {
    sid       = "InvokeTarget"
    actions   = var.target_actions
    resources = [var.target_arn]
  }

  dynamic "statement" {
    for_each = var.dead_letter_queue_arn == null ? [] : [var.dead_letter_queue_arn]

    content {
      sid       = "DeadLetter"
      actions   = ["sqs:SendMessage"]
      resources = [statement.value]
    }
  }

  statement {
    sid       = "ScheduleKey"
    actions   = ["kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "invoke" {
  name   = "invoke"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.invoke.json
}

resource "aws_scheduler_schedule" "this" {
  name                         = var.name
  description                  = var.description
  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = var.timezone
  state                        = var.enabled ? "ENABLED" : "DISABLED"
  kms_key_arn                  = var.kms_key_arn

  flexible_time_window {
    mode                      = var.flexible_window_minutes == null ? "OFF" : "FLEXIBLE"
    maximum_window_in_minutes = var.flexible_window_minutes
  }

  target {
    arn      = var.target_arn
    role_arn = aws_iam_role.this.arn
    input    = var.input

    retry_policy {
      maximum_retry_attempts       = var.retry_attempts
      maximum_event_age_in_seconds = var.max_event_age_seconds
    }

    dynamic "dead_letter_config" {
      for_each = var.dead_letter_queue_arn == null ? [] : [var.dead_letter_queue_arn]

      content {
        arn = dead_letter_config.value
      }
    }
  }

  depends_on = [aws_iam_role_policy.invoke]
}
