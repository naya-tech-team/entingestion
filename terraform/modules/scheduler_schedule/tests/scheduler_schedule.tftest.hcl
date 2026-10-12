mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }

  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}

variables {
  name                = "reconciler"
  schedule_expression = "rate(1 hour)"
  target_arn          = "arn:aws:lambda:us-west-2:123456789012:function:reconciler"
  kms_key_arn         = "arn:aws:kms:us-west-2:123456789012:key/abc"
}

run "hourly_lambda_target" {
  command = plan

  assert {
    condition     = aws_scheduler_schedule.this.state == "ENABLED" && aws_scheduler_schedule.this.schedule_expression == "rate(1 hour)"
    error_message = "The schedule must be enabled with the given expression."
  }

  assert {
    condition     = aws_scheduler_schedule.this.flexible_time_window[0].mode == "OFF"
    error_message = "No flexible window by default."
  }

  assert {
    condition     = aws_scheduler_schedule.this.kms_key_arn == var.kms_key_arn
    error_message = "The schedule must be encrypted with the given key."
  }

  assert {
    condition     = length(aws_scheduler_schedule.this.target[0].dead_letter_config) == 0
    error_message = "No DLQ unless one is given."
  }
}

run "role_is_scoped_to_the_target" {
  command = plan

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.invoke.statement :
      s.sid != "InvokeTarget" || (tolist(s.resources) == tolist([var.target_arn]) && tolist(s.actions) == tolist(["lambda:InvokeFunction"]))
    ])
    error_message = "The scheduler role may only invoke the target."
  }

  assert {
    condition     = one([for s in data.aws_iam_policy_document.assume.statement : s.condition]) != null
    error_message = "The trust policy must pin the source account."
  }
}

run "disabled_with_window_and_dlq" {
  command = plan

  variables {
    enabled                 = false
    flexible_window_minutes = 15
    dead_letter_queue_arn   = "arn:aws:sqs:us-west-2:123456789012:dlq"
  }

  assert {
    condition     = aws_scheduler_schedule.this.state == "DISABLED"
    error_message = "enabled = false must disable the schedule."
  }

  assert {
    condition     = aws_scheduler_schedule.this.flexible_time_window[0].mode == "FLEXIBLE" && aws_scheduler_schedule.this.flexible_time_window[0].maximum_window_in_minutes == 15
    error_message = "flexible_window_minutes must set a FLEXIBLE window."
  }

  assert {
    condition     = aws_scheduler_schedule.this.target[0].dead_letter_config[0].arn == var.dead_letter_queue_arn
    error_message = "The DLQ must be wired."
  }
}

run "rejects_bad_expressions" {
  command = plan

  variables {
    schedule_expression = "every hour"
  }

  expect_failures = [var.schedule_expression]
}

run "roles_carry_the_permissions_boundary" {
  command = plan

  variables {
    permissions_boundary = "arn:aws:iam::123456789012:policy/entingestion-dev-workload-boundary"
  }

  assert {
    condition     = aws_iam_role.this.permissions_boundary == var.permissions_boundary && output.role_permissions_boundaries == [var.permissions_boundary]
    error_message = "Every role the module creates must carry the given permissions boundary."
  }
}
