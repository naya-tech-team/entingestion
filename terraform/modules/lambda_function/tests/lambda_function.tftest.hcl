mock_provider "aws" {
  override_during = plan

  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-west-2:123456789012:log-group:/aws/lambda/fn" }
  }
}

variables {
  name        = "fn"
  handler     = "app.handler"
  timeout     = 29
  code        = { zip_path = "fn.zip", source_code_hash = "abc=" }
  policy_json = "{}"
  kms_key_arn = "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000"
}

run "defaults" {
  command = plan

  assert {
    condition     = aws_lambda_function.this.architectures == tolist(["arm64"]) && aws_lambda_function.this.runtime == "python3.12"
    error_message = "Default must be python3.12 on arm64."
  }

  assert {
    condition     = aws_lambda_function.this.tracing_config[0].mode == "Active"
    error_message = "X-Ray tracing must be Active."
  }

  assert {
    condition     = aws_cloudwatch_log_group.this.name == "/aws/lambda/fn" && aws_cloudwatch_log_group.this.retention_in_days == 30
    error_message = "Log group must be /aws/lambda/<name> with retention."
  }

  assert {
    condition     = aws_cloudwatch_log_group.this.kms_key_id == var.kms_key_arn && aws_lambda_function.this.kms_key_arn == var.kms_key_arn
    error_message = "Log group and environment must use the given KMS key."
  }

  assert {
    condition     = length(aws_lambda_function.this.environment) == 0
    error_message = "No environment block when no variables are given."
  }
}

run "callers_use_a_published_alias" {
  command = plan

  assert {
    condition = (
      aws_lambda_function.this.publish
      && aws_lambda_alias.live.name == "live"
      && aws_lambda_alias.live.function_name == "fn"
    )
    error_message = "Every change publishes a version, and the live alias points at it (rollback = move the alias)."
  }
}

run "logs_scoped_to_own_group" {
  command = plan

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.runtime.statement :
      s.sid == "WriteOwnLogs" && s.resources == toset(["arn:aws:logs:us-west-2:123456789012:log-group:/aws/lambda/fn:*"])
    ])
    error_message = "Log permissions must be scoped to the function's log group."
  }
}

run "s3_code_source" {
  command = plan

  variables {
    code = { s3_bucket = "artifacts", s3_key = "producer/abc.zip", source_code_hash = "abc=" }
  }

  assert {
    condition     = aws_lambda_function.this.s3_bucket == "artifacts" && aws_lambda_function.this.filename == null
    error_message = "S3 code source must be passed through."
  }
}

run "rejects_both_code_sources" {
  command = plan

  variables {
    code = { zip_path = "fn.zip", s3_bucket = "artifacts", s3_key = "k", source_code_hash = "abc=" }
  }

  expect_failures = [var.code]
}

run "rejects_s3_bucket_without_key" {
  command = plan

  variables {
    code = { s3_bucket = "artifacts", source_code_hash = "abc=" }
  }

  expect_failures = [var.code]
}

run "zip_functions_stay_outside_a_vpc" {
  command = plan

  assert {
    condition = (
      aws_lambda_function.this.package_type == "Zip"
      && length(aws_lambda_function.this.vpc_config) == 0
      && length(aws_iam_role_policy_attachment.vpc_access) == 0
    )
    error_message = "Defaults must be a zip package outside any VPC."
  }
}

run "vpc_config_adds_eni_permissions" {
  command = plan

  variables {
    vpc_config = { subnet_ids = ["subnet-1", "subnet-2"], security_group_ids = ["sg-1"] }
  }

  assert {
    condition     = one(aws_lambda_function.this.vpc_config).subnet_ids == toset(["subnet-1", "subnet-2"])
    error_message = "vpc_config must be passed through."
  }

  assert {
    condition     = endswith(aws_iam_role_policy_attachment.vpc_access[0].policy_arn, "AWSLambdaVPCAccessExecutionRole")
    error_message = "VPC functions need the ENI permissions."
  }
}

run "image_package_uses_handler_as_command" {
  command = plan

  variables {
    code = { image_uri = "123456789012.dkr.ecr.us-west-2.amazonaws.com/ingest@sha256:abc" }
  }

  assert {
    condition = (
      aws_lambda_function.this.package_type == "Image"
      && aws_lambda_function.this.handler == null
      && aws_lambda_function.this.runtime == null
      && one(aws_lambda_function.this.image_config).command == tolist(["app.handler"])
    )
    error_message = "Image functions must not set handler/runtime and must use the handler as the command."
  }
}

run "rejects_zip_without_hash" {
  command = plan

  variables {
    code = { zip_path = "fn.zip" }
  }

  expect_failures = [var.code]
}

run "rejects_empty_vpc_config" {
  command = plan

  variables {
    vpc_config = { subnet_ids = [], security_group_ids = ["sg-1"] }
  }

  expect_failures = [var.vpc_config]
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
