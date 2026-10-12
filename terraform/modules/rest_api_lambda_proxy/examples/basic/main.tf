terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  description = "Region to deploy the example in."
  type        = string
  default     = "us-west-2"
}

variable "name" {
  description = "Name prefix for the example's resources."
  type        = string
  default     = "example"
}

module "kms" {
  source      = "../../../kms_key"
  alias       = "${var.name}-example"
  description = "Example key"
}

data "archive_file" "code" {
  type        = "zip"
  output_path = "${path.module}/build/example.zip"

  source {
    filename = "app.py"
    content  = "def handler(event, context):\n    return {'statusCode': 200, 'body': 'ok'}\n"
  }
}

data "aws_iam_policy_document" "none" {
  statement {
    sid       = "NoExtraPermissions"
    effect    = "Deny"
    actions   = ["s3:DeleteBucket"]
    resources = ["*"]
  }
}

module "function" {
  source      = "../../../lambda_function"
  name        = "${var.name}-example-api"
  handler     = "app.handler"
  code        = { zip_path = data.archive_file.code.output_path, source_code_hash = data.archive_file.code.output_base64sha256 }
  policy_json = data.aws_iam_policy_document.none.json
  kms_key_arn = module.kms.key_arn
}

module "api" {
  source               = "../.."
  name                 = "${var.name}-example"
  lambda_function_name = module.function.function_name
  lambda_invoke_arn    = module.function.invoke_arn
  log_kms_key_arn      = module.kms.key_arn
  invoke_policy_name   = "${var.name}-example-invoke"
}

output "invoke_url" {
  description = "Stage URL (SigV4-signed requests only)."
  value       = module.api.invoke_url
}

# APIs should sit behind WAF (AWS managed rules include Log4j/known-bad-input protection).
module "waf" {
  source          = "../../../waf_web_acl"
  name            = "${var.name}-example-api"
  resource_arns   = { api = module.api.stage_arn }
  log_kms_key_arn = module.kms.key_arn

  managed_rule_groups = [
    { name = "AWSManagedRulesAmazonIpReputationList", priority = 10 },
    { name = "AWSManagedRulesKnownBadInputsRuleSet", priority = 20 },
    { name = "AWSManagedRulesCommonRuleSet", priority = 30 },
  ]
}
