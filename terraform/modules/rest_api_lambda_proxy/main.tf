data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

resource "aws_api_gateway_rest_api" "this" {
  name        = var.name
  description = var.description

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  lifecycle {
    create_before_destroy = true
  }
}

# Every path goes to one Lambda: the root plus {proxy+}, both IAM-authenticated.
resource "aws_api_gateway_resource" "proxy" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_rest_api.this.root_resource_id
  path_part   = "{proxy+}"
}

resource "aws_api_gateway_method" "root" {
  #checkov:skip=CKV2_AWS_53:{proxy+} can't use API Gateway request models; the Lambda validates every body strictly (unknown fields -> 422).
  rest_api_id   = aws_api_gateway_rest_api.this.id
  resource_id   = aws_api_gateway_rest_api.this.root_resource_id
  http_method   = "ANY"
  authorization = "AWS_IAM"
}

resource "aws_api_gateway_method" "proxy" {
  #checkov:skip=CKV2_AWS_53:{proxy+} can't use API Gateway request models; the Lambda validates every body strictly (unknown fields -> 422).
  rest_api_id   = aws_api_gateway_rest_api.this.id
  resource_id   = aws_api_gateway_resource.proxy.id
  http_method   = "ANY"
  authorization = "AWS_IAM"
}

resource "aws_api_gateway_integration" "root" {
  rest_api_id             = aws_api_gateway_rest_api.this.id
  resource_id             = aws_api_gateway_rest_api.this.root_resource_id
  http_method             = aws_api_gateway_method.root.http_method
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = var.lambda_invoke_arn
}

resource "aws_api_gateway_integration" "proxy" {
  rest_api_id             = aws_api_gateway_rest_api.this.id
  resource_id             = aws_api_gateway_resource.proxy.id
  http_method             = aws_api_gateway_method.proxy.http_method
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = var.lambda_invoke_arn
}

resource "aws_api_gateway_deployment" "this" {
  rest_api_id = aws_api_gateway_rest_api.this.id

  triggers = {
    redeploy = sha1(jsonencode([
      aws_api_gateway_resource.proxy.id,
      aws_api_gateway_method.root.id,
      aws_api_gateway_method.root.authorization,
      aws_api_gateway_method.proxy.id,
      aws_api_gateway_method.proxy.authorization,
      aws_api_gateway_integration.root.id,
      aws_api_gateway_integration.root.uri,
      aws_api_gateway_integration.proxy.id,
      aws_api_gateway_integration.proxy.uri,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [aws_api_gateway_integration.root, aws_api_gateway_integration.proxy]
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "access" {
  #checkov:skip=CKV_AWS_338:Retention is set per environment (brief §7.3: 30 days dev, 90 days prod).
  name              = "/aws/apigateway/${var.name}/${var.stage_name}/access"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_kms_key_arn
}

# API Gateway writes execution logs to this fixed name; pre-creating it sets retention and KMS.
resource "aws_cloudwatch_log_group" "execution" {
  #checkov:skip=CKV_AWS_338:Retention is set per environment (brief §7.3: 30 days dev, 90 days prod).
  name              = "API-Gateway-Execution-Logs_${aws_api_gateway_rest_api.this.id}/${var.stage_name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_kms_key_arn
}

data "aws_iam_policy_document" "apigw_assume" {
  count = var.manage_apigw_account ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["apigateway.amazonaws.com"]
    }

    # Confused-deputy guard. The account-level logging role has no single source ARN.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "apigw_cloudwatch" {
  count                = var.manage_apigw_account ? 1 : 0
  name                 = "apigateway-cloudwatch-logs"
  assume_role_policy   = data.aws_iam_policy_document.apigw_assume[0].json
  permissions_boundary = var.permissions_boundary
}

resource "aws_iam_role_policy_attachment" "apigw_cloudwatch" {
  count      = var.manage_apigw_account ? 1 : 0
  role       = aws_iam_role.apigw_cloudwatch[0].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

resource "aws_api_gateway_account" "this" {
  count               = var.manage_apigw_account ? 1 : 0
  cloudwatch_role_arn = aws_iam_role.apigw_cloudwatch[0].arn

  depends_on = [aws_iam_role_policy_attachment.apigw_cloudwatch]
}

# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

resource "aws_api_gateway_stage" "this" {
  #checkov:skip=CKV_AWS_120:Responses are per-caller and change with every write; caching would serve stale data.
  #checkov:skip=CKV2_AWS_51:Callers authenticate with SigV4 (AWS_IAM); client certificates add nothing.
  #checkov:skip=CKV2_AWS_29:WAF is attached by the consuming stack (waf_web_acl module, Phase 3).
  #checkov:skip=CKV2_AWS_77:waf_web_acl's managed groups include AWSManagedRulesKnownBadInputsRuleSet (Log4j); checkov can't read the module's dynamic rule blocks.
  rest_api_id          = aws_api_gateway_rest_api.this.id
  deployment_id        = aws_api_gateway_deployment.this.id
  stage_name           = var.stage_name
  xray_tracing_enabled = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.access.arn
    format = jsonencode({
      requestId        = "$context.requestId"
      ip               = "$context.identity.sourceIp"
      caller           = "$context.identity.caller"
      user             = "$context.identity.user"
      userArn          = "$context.identity.userArn"
      requestTime      = "$context.requestTime"
      httpMethod       = "$context.httpMethod"
      resourcePath     = "$context.resourcePath"
      path             = "$context.path"
      status           = "$context.status"
      protocol         = "$context.protocol"
      responseLength   = "$context.responseLength"
      latencyMs        = "$context.responseLatency"
      integrationError = "$context.integration.error"
    })
  }

  depends_on = [aws_cloudwatch_log_group.execution, aws_api_gateway_account.this]
}

resource "aws_api_gateway_method_settings" "all" {
  #checkov:skip=CKV_AWS_225:Caching is intentionally off (see the stage).
  rest_api_id = aws_api_gateway_rest_api.this.id
  stage_name  = aws_api_gateway_stage.this.stage_name
  method_path = "*/*"

  settings {
    metrics_enabled        = true
    logging_level          = var.logging_level
    data_trace_enabled     = false # full request/response bodies would leak caller data into logs
    throttling_rate_limit  = var.throttle_rate_limit
    throttling_burst_limit = var.throttle_burst_limit
  }
}

# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = var.lambda_function_name
  qualifier     = var.lambda_qualifier
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.this.execution_arn}/*/*"
}

data "aws_iam_policy_document" "invoke" {
  statement {
    sid       = "InvokeApi"
    actions   = ["execute-api:Invoke"]
    resources = [for p in var.invoke_resource_paths : "${aws_api_gateway_rest_api.this.execution_arn}/${var.stage_name}/${p}"]
  }
}

resource "aws_iam_policy" "invoke" {
  name        = var.invoke_policy_name
  description = "Lets callers invoke the ${var.name} API (${var.stage_name} stage)."
  policy      = data.aws_iam_policy_document.invoke.json
}
