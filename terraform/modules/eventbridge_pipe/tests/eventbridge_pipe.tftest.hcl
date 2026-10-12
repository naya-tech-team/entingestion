mock_provider "aws" {
  override_during = plan

  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-west-2:123456789012:log-group:/aws/vendedlogs/pipes/p" }
  }
}

variables {
  name              = "p"
  source_stream_arn = "arn:aws:dynamodb:us-west-2:123456789012:table/articles/stream/2026"
  target_queue_arn  = "arn:aws:sqs:us-west-2:123456789012:ingest"
  dlq_arn           = "arn:aws:sqs:us-west-2:123456789012:pipe-dlq"
  kms_key_arn       = "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000"
  filter_patterns   = ["{\"eventName\":[\"REMOVE\"]}", "{\"eventName\":[\"INSERT\"]}"]
}

run "stream_to_queue_with_dlq_and_filters" {
  command = plan

  assert {
    condition = (
      one(one(aws_pipes_pipe.this.source_parameters).dynamodb_stream_parameters).starting_position == "LATEST"
      && one(one(aws_pipes_pipe.this.source_parameters).dynamodb_stream_parameters).maximum_retry_attempts == 5
      && one(one(one(aws_pipes_pipe.this.source_parameters).dynamodb_stream_parameters).dead_letter_config).arn == var.dlq_arn
    )
    error_message = "Pipe must start at LATEST, retry 5 times, then use the DLQ."
  }

  assert {
    condition     = length(one(one(aws_pipes_pipe.this.source_parameters).filter_criteria).filter) == 2
    error_message = "Every filter pattern must be applied."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.this.statement :
      s.sid == "ReadSourceStream" && s.resources == toset([var.source_stream_arn])
    ])
    error_message = "Pipe role may only read its source stream."
  }
}

run "rejects_invalid_filter" {
  command = plan

  variables {
    filter_patterns = ["{not json"]
  }

  expect_failures = [var.filter_patterns]
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
