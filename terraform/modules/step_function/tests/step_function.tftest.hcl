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
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-west-2:123456789012:log-group:/aws/vendedlogs/states/sm" }
  }
}

variables {
  name        = "sm"
  definition  = "{\"StartAt\":\"Done\",\"States\":{\"Done\":{\"Type\":\"Succeed\"}}}"
  kms_key_arn = "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000"
}

run "standard_encrypted_traced_logged" {
  command = plan

  assert {
    condition = (
      aws_sfn_state_machine.this.type == "STANDARD"
      && one(aws_sfn_state_machine.this.tracing_configuration).enabled
      && one(aws_sfn_state_machine.this.encryption_configuration).kms_key_id == var.kms_key_arn
      && one(aws_sfn_state_machine.this.logging_configuration).level == "ALL"
      && !one(aws_sfn_state_machine.this.logging_configuration).include_execution_data
    )
    error_message = "State machine must be STANDARD, traced, CMK-encrypted and logged without execution data."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.this.statement :
      s.sid == "ExecutionDataKey" && anytrue([for c in s.condition : contains(c.values, "arn:aws:states:us-west-2:123456789012:stateMachine:sm")])
    ])
    error_message = "Key use must be scoped to this state machine's encryption context."
  }
}

run "rejects_non_asl" {
  command = plan

  variables {
    definition = "{}"
  }

  expect_failures = [var.definition]
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
