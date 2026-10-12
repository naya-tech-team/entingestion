# BFF: the UI's "static user" (services/ui/bff). CloudFront reaches it through a function URL
# with AWS_IAM auth and a Lambda OAC; it calls the two APIs signed as its own role.

locals {
  # The upstream routes the BFF may call, per API and method (its allow-list, docs/UI.md). In an
  # execute-api ARN, * also matches /, so these are as narrow as the paths allow. No DELETE or
  # PATCH, no tables, no reindex.
  producer_routes = {
    GET  = ["/v1/sources", "/v1/processes", "/v1/processes/*", "/v1/processes/*/uploads"]
    POST = ["/v1/processes", "/v1/processes/*/close", "/v1/processes/*/uploads"]
  }
  control_routes = {
    GET = ["/v1/processes/*/summary", "/v1/processes/*/articles", "/v1/articles/*/status"]
  }

  # <execution arn>/<stage>/<METHOD>/<path>
  bff_invoke_arns = {
    producer = flatten([for method, paths in local.producer_routes : [for p in paths : "${local.upstream.producer_api_execution_arn}/*/${method}${p}"]])
    control  = flatten([for method, paths in local.control_routes : [for p in paths : "${local.upstream.control_api_execution_arn}/*/${method}${p}"]])
  }
}

data "aws_iam_policy_document" "bff" {
  # Which sources it may use is decided by the producer (source_access lists this role).
  statement {
    sid       = "InvokeProducerApi"
    actions   = ["execute-api:Invoke"]
    resources = local.bff_invoke_arns.producer
  }

  statement {
    sid       = "InvokeControlApiReadOnly"
    actions   = ["execute-api:Invoke"]
    resources = local.bff_invoke_arns.control
  }
}

module "bff" {
  source = "../../modules/lambda_function"

  name        = "${local.name_prefix}-bff"
  description = "Upload UI backend (${var.env}): allow-listed, signed calls to the producer and control APIs"
  handler     = "bff.app.handler"
  memory_size = var.bff_memory_size
  # CloudFront waits 30 s for the origin; the BFF gives up on an API after 3 s + 25 s.
  timeout = 29
  code    = local.lambda_code
  environment = {
    PRODUCER_API_URL        = local.upstream.producer_api_url
    CONTROL_API_URL         = local.upstream.control_api_url
    POWERTOOLS_SERVICE_NAME = "ui-bff"
    POWERTOOLS_LOG_LEVEL    = var.log_level
  }
  policy_json = data.aws_iam_policy_document.bff.json
  # The producer's key (its policy already lets CloudWatch Logs use it): no extra $1/month key
  # for a log group and four non-secret environment variables.
  kms_key_arn          = local.upstream.producer_kms_key_arn
  log_retention_days   = var.log_retention_days
  permissions_boundary = local.permissions_boundary
}

# Only CloudFront calls it, signed by the Lambda OAC; the URL rejects unsigned requests.
resource "aws_lambda_function_url" "bff" {
  function_name      = module.bff.function_name
  qualifier          = module.bff.alias_name
  authorization_type = "AWS_IAM"
}

# Since October 2025 a function URL caller needs both lambda:InvokeFunctionUrl and
# lambda:InvokeFunction (the latter only through the URL). Both are for this distribution only.
resource "aws_lambda_permission" "cloudfront_invoke_url" {
  statement_id           = "CloudFrontInvokeFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = module.bff.function_name
  qualifier              = module.bff.alias_name
  principal              = "cloudfront.amazonaws.com"
  source_arn             = aws_cloudfront_distribution.this.arn
  function_url_auth_type = "AWS_IAM"
}

resource "aws_lambda_permission" "cloudfront_invoke" {
  statement_id             = "CloudFrontInvokeFunctionViaUrl"
  action                   = "lambda:InvokeFunction"
  function_name            = module.bff.function_name
  qualifier                = module.bff.alias_name
  principal                = "cloudfront.amazonaws.com"
  source_arn               = aws_cloudfront_distribution.this.arn
  invoked_via_function_url = true
}
