data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

resource "aws_sns_topic" "this" {
  name              = var.topic_name
  kms_master_key_id = var.kms_key_arn
}

# CloudWatch alarms in this account may publish; nothing else outside the account can.
data "aws_iam_policy_document" "topic" {
  statement {
    sid       = "CloudWatchAlarms"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.this.arn]

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  statement {
    sid = "AccountOwner"
    actions = [
      "sns:GetTopicAttributes",
      "sns:SetTopicAttributes",
      "sns:Subscribe",
      "sns:ListSubscriptionsByTopic",
      "sns:Publish",
    ]
    resources = [aws_sns_topic.this.arn]

    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

resource "aws_sns_topic_policy" "this" {
  arn    = aws_sns_topic.this.arn
  policy = data.aws_iam_policy_document.topic.json
}

resource "aws_sns_topic_subscription" "email" {
  for_each = toset(var.email_endpoints)

  topic_arn = aws_sns_topic.this.arn
  protocol  = "email"
  endpoint  = each.value
}

resource "aws_cloudwatch_metric_alarm" "this" {
  for_each = var.alarms

  alarm_name          = "${var.topic_name}-${each.key}"
  alarm_description   = each.value.description
  threshold           = each.value.threshold
  evaluation_periods  = each.value.evaluation_periods
  datapoints_to_alarm = each.value.datapoints_to_alarm
  comparison_operator = each.value.comparison_operator
  treat_missing_data  = each.value.treat_missing_data

  # One metric ...
  namespace          = each.value.expression == null ? each.value.namespace : null
  metric_name        = each.value.expression == null ? each.value.metric_name : null
  dimensions         = each.value.expression == null ? each.value.dimensions : null
  statistic          = each.value.expression == null ? each.value.statistic : null
  extended_statistic = each.value.expression == null ? each.value.extended_statistic : null
  period             = each.value.expression == null ? each.value.period : null

  # ... or metric math over several.
  dynamic "metric_query" {
    for_each = each.value.expression == null ? {} : { e1 = each.value.expression }

    content {
      id          = metric_query.key
      expression  = metric_query.value
      label       = each.key
      return_data = true
    }
  }

  dynamic "metric_query" {
    for_each = each.value.expression == null ? {} : each.value.metrics

    content {
      id          = metric_query.key
      return_data = false

      metric {
        namespace   = metric_query.value.namespace
        metric_name = metric_query.value.metric_name
        dimensions  = metric_query.value.dimensions
        stat        = metric_query.value.stat
        period      = each.value.period
      }
    }
  }

  alarm_actions = [aws_sns_topic.this.arn]
  ok_actions    = var.notify_on_ok ? [aws_sns_topic.this.arn] : []
}
