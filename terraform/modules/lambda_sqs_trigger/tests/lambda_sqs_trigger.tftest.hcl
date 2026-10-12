mock_provider "aws" {}

variables {
  function_name = "fn"
  queue_arn     = "arn:aws:sqs:us-west-2:123456789012:q"
}

run "partial_batch_responses_always_on" {
  command = plan

  assert {
    condition     = contains(aws_lambda_event_source_mapping.this.function_response_types, "ReportBatchItemFailures")
    error_message = "ReportBatchItemFailures must always be on."
  }

  assert {
    condition     = aws_lambda_event_source_mapping.this.batch_size == 10 && length(aws_lambda_event_source_mapping.this.scaling_config) == 0
    error_message = "Defaults: batch 10, no concurrency bound."
  }
}

run "bounded_concurrency" {
  command = plan

  variables {
    max_concurrency         = 2
    batching_window_seconds = 5
  }

  assert {
    condition     = one(aws_lambda_event_source_mapping.this.scaling_config).maximum_concurrency == 2
    error_message = "max_concurrency must become scaling_config.maximum_concurrency."
  }
}

run "rejects_concurrency_below_two" {
  command = plan

  variables {
    max_concurrency = 1
  }

  expect_failures = [var.max_concurrency]
}
