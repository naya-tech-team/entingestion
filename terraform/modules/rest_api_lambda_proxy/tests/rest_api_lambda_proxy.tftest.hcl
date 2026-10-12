mock_provider "aws" {
  override_during = plan

  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_resource "aws_api_gateway_rest_api" {
    defaults = {
      id               = "abc123"
      root_resource_id = "root01"
      execution_arn    = "arn:aws:execute-api:us-west-2:123456789012:abc123"
    }
  }
}

variables {
  name                  = "api"
  lambda_function_name  = "fn"
  lambda_invoke_arn     = "arn:aws:apigateway:us-west-2:lambda:path/2015-03-31/functions/arn:aws:lambda:us-west-2:123456789012:function:fn/invocations"
  log_kms_key_arn       = "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000"
  invoke_policy_name    = "api-invoke"
  invoke_resource_paths = ["*/v1/*"]
}

run "both_methods_use_iam_auth" {
  command = plan

  assert {
    condition     = aws_api_gateway_method.root.authorization == "AWS_IAM" && aws_api_gateway_method.proxy.authorization == "AWS_IAM"
    error_message = "Root and {proxy+} methods must use AWS_IAM."
  }

  assert {
    condition     = aws_api_gateway_method.root.http_method == "ANY" && aws_api_gateway_method.proxy.http_method == "ANY"
    error_message = "Both methods must be ANY."
  }

  assert {
    condition     = aws_api_gateway_integration.root.type == "AWS_PROXY" && aws_api_gateway_integration.proxy.type == "AWS_PROXY"
    error_message = "Both integrations must be AWS_PROXY."
  }
}

run "regional_stage_with_logs_tracing_and_throttling" {
  command = plan

  assert {
    condition     = aws_api_gateway_rest_api.this.endpoint_configuration[0].types == tolist(["REGIONAL"])
    error_message = "API must be REGIONAL."
  }

  assert {
    condition     = aws_api_gateway_stage.this.stage_name == "prod" && aws_api_gateway_stage.this.xray_tracing_enabled
    error_message = "Stage must be prod with X-Ray."
  }

  assert {
    condition     = length(aws_api_gateway_stage.this.access_log_settings) == 1
    error_message = "Stage must have access logs."
  }

  assert {
    condition = (
      aws_api_gateway_method_settings.all.method_path == "*/*"
      && aws_api_gateway_method_settings.all.settings[0].throttling_rate_limit == 50
      && aws_api_gateway_method_settings.all.settings[0].throttling_burst_limit == 100
      && aws_api_gateway_method_settings.all.settings[0].metrics_enabled
      && !aws_api_gateway_method_settings.all.settings[0].data_trace_enabled
    )
    error_message = "Method settings must enable metrics and throttling without data tracing."
  }

  assert {
    condition     = aws_cloudwatch_log_group.execution.name == "API-Gateway-Execution-Logs_abc123/prod"
    error_message = "Execution log group must use API Gateway's fixed name."
  }
}

run "invoke_policy_and_lambda_permission" {
  command = plan

  assert {
    condition     = aws_lambda_permission.apigw.source_arn == "arn:aws:execute-api:us-west-2:123456789012:abc123/*/*"
    error_message = "Lambda permission must be scoped to this API."
  }

  assert {
    condition     = aws_lambda_permission.apigw.qualifier == null
    error_message = "Without lambda_qualifier the permission is on the unqualified function."
  }
}

run "an_alias_gets_the_invoke_permission" {
  command = plan

  variables {
    lambda_invoke_arn = "arn:aws:apigateway:us-west-2:lambda:path/2015-03-31/functions/arn:aws:lambda:us-west-2:123456789012:function:fn:live/invocations"
    lambda_qualifier  = "live"
  }

  assert {
    condition = (
      aws_lambda_permission.apigw.qualifier == "live"
      && aws_api_gateway_integration.proxy.uri == var.lambda_invoke_arn
    )
    error_message = "API Gateway must invoke, and be allowed to invoke, the alias."
  }

  assert {
    condition = one(data.aws_iam_policy_document.invoke.statement).resources == toset([
      "arn:aws:execute-api:us-west-2:123456789012:abc123/prod/*/v1/*",
    ])
    error_message = "Invoke policy must cover only the configured stage paths."
  }
}

run "account_role_is_opt_in" {
  command = plan

  assert {
    condition     = length(aws_api_gateway_account.this) == 0 && length(aws_iam_role.apigw_cloudwatch) == 0
    error_message = "The account-level CloudWatch role must not be managed by default."
  }
}

run "account_role_when_enabled" {
  command = plan

  variables {
    manage_apigw_account = true
  }

  assert {
    condition     = length(aws_api_gateway_account.this) == 1
    error_message = "manage_apigw_account must create aws_api_gateway_account."
  }

  assert {
    condition = anytrue([
      for c in one(data.aws_iam_policy_document.apigw_assume[0].statement).condition :
      c.variable == "aws:SourceAccount" && c.values == tolist(["123456789012"])
    ])
    error_message = "Only API Gateway in this account may assume the logging role."
  }
}

run "roles_carry_the_permissions_boundary" {
  command = plan

  variables {
    manage_apigw_account = true
    permissions_boundary = "arn:aws:iam::123456789012:policy/entingestion-dev-workload-boundary"
  }

  assert {
    condition     = aws_iam_role.apigw_cloudwatch[0].permissions_boundary == var.permissions_boundary && output.role_permissions_boundaries == [var.permissions_boundary]
    error_message = "Every role the module creates must carry the given permissions boundary."
  }
}
