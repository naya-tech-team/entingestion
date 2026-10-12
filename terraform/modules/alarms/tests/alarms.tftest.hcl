mock_provider "aws" {
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
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-west-2:123456789012:alarms" }
  }
}

variables {
  topic_name  = "alarms"
  kms_key_arn = "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000"
  alarms = {
    errors = {
      description         = "errors"
      namespace           = "AWS/Lambda"
      metric_name         = "Errors"
      dimensions          = { FunctionName = "fn" }
      statistic           = "Sum"
      threshold           = 1
      evaluation_periods  = 3
      datapoints_to_alarm = 3
    }
  }
}

run "topic_and_alarm" {
  command = plan

  assert {
    condition     = aws_sns_topic.this.kms_master_key_id == var.kms_key_arn
    error_message = "Topic must be encrypted with the given key."
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.this["errors"].alarm_name == "alarms-errors"
      && aws_cloudwatch_metric_alarm.this["errors"].evaluation_periods == 3
      && aws_cloudwatch_metric_alarm.this["errors"].treat_missing_data == "notBreaching"
      && aws_cloudwatch_metric_alarm.this["errors"].comparison_operator == "GreaterThanOrEqualToThreshold"
    )
    error_message = "Alarm must be passed through with defaults."
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.this["errors"].alarm_actions == toset(["arn:aws:sns:us-west-2:123456789012:alarms"])
      && length(aws_cloudwatch_metric_alarm.this["errors"].ok_actions) == 0
    )
    error_message = "Alarms notify the topic on ALARM only by default (one message per incident)."
  }

  assert {
    condition     = length(aws_sns_topic_subscription.email) == 0
    error_message = "No subscriptions without email_endpoints."
  }
}

run "cloudwatch_publish_is_account_scoped" {
  command = plan

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.topic.statement :
      s.sid == "CloudWatchAlarms" && anytrue([for c in s.condition : c.variable == "aws:SourceAccount"])
    ])
    error_message = "CloudWatch publish must be restricted to this account."
  }
}

run "rejects_unknown_statistic" {
  command = plan

  variables {
    alarms = {
      bad = {
        description = "bad"
        namespace   = "X"
        metric_name = "Y"
        dimensions  = {}
        statistic   = "p99"
        threshold   = 1
      }
    }
  }

  expect_failures = [var.alarms]
}

run "percentile_alarm" {
  command = plan

  variables {
    alarms = {
      slow = {
        description        = "slow"
        namespace          = "X"
        metric_name        = "Duration"
        dimensions         = {}
        extended_statistic = "p95"
        threshold          = 1000
      }
    }
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.this["slow"].extended_statistic == "p95"
      && aws_cloudwatch_metric_alarm.this["slow"].statistic == null
    )
    error_message = "A percentile alarm must use extended_statistic only."
  }
}

run "rejects_both_statistics" {
  command = plan

  variables {
    alarms = {
      bad = {
        description        = "bad"
        namespace          = "X"
        metric_name        = "Y"
        dimensions         = {}
        statistic          = "Sum"
        extended_statistic = "p95"
        threshold          = 1
      }
    }
  }

  expect_failures = [var.alarms]
}

run "rejects_a_malformed_percentile" {
  command = plan

  variables {
    alarms = {
      bad = {
        description        = "bad"
        namespace          = "X"
        metric_name        = "Y"
        dimensions         = {}
        extended_statistic = "95th"
        threshold          = 1
      }
    }
  }

  expect_failures = [var.alarms]
}

run "ok_notifications_are_opt_in" {
  command = plan

  variables {
    notify_on_ok = true
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.this["errors"].ok_actions == toset(["arn:aws:sns:us-west-2:123456789012:alarms"])
    error_message = "notify_on_ok = true must also notify on OK."
  }
}

run "metric_math_alarm" {
  command = plan

  variables {
    alarms = {
      ratio = {
        description = "failure ratio"
        expression  = "IF(ok + failed >= 20, 100 * failed / (ok + failed), 0)"
        metrics = {
          ok     = { namespace = "X", metric_name = "Run_INDEXED", dimensions = { service = "x" }, stat = "Sum" }
          failed = { namespace = "X", metric_name = "Run_FAILED_PERMANENT", dimensions = { service = "x" }, stat = "Sum" }
        }
        threshold = 1
        period    = 3600
      }
    }
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.this["ratio"].metric_name == null
      && aws_cloudwatch_metric_alarm.this["ratio"].statistic == null
      && length(aws_cloudwatch_metric_alarm.this["ratio"].metric_query) == 3
      && anytrue([for q in aws_cloudwatch_metric_alarm.this["ratio"].metric_query : q.id == "e1" && q.return_data])
      && alltrue([for q in aws_cloudwatch_metric_alarm.this["ratio"].metric_query : q.id == "e1" || (!q.return_data && one(q.metric).period == 3600)])
    )
    error_message = "An expression alarm returns only the expression, over metrics at the alarm's period."
  }
}

run "rejects_an_expression_with_a_statistic" {
  command = plan

  variables {
    alarms = {
      bad = {
        description = "bad"
        expression  = "m1"
        statistic   = "Sum"
        metrics     = { m1 = { namespace = "X", metric_name = "Y", stat = "Sum" } }
        threshold   = 1
      }
    }
  }

  expect_failures = [var.alarms]
}

run "rejects_an_alarm_without_a_metric" {
  command = plan

  variables {
    alarms = {
      bad = {
        description = "bad"
        statistic   = "Sum"
        threshold   = 1
      }
    }
  }

  expect_failures = [var.alarms]
}
