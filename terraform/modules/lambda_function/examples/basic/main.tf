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
    content  = "def handler(event, context):\n    return {'ok': True}\n"
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
  source      = "../.."
  name        = "${var.name}-example"
  handler     = "app.handler"
  code        = { zip_path = data.archive_file.code.output_path, source_code_hash = data.archive_file.code.output_base64sha256 }
  policy_json = data.aws_iam_policy_document.none.json
  kms_key_arn = module.kms.key_arn
}

output "function_name" {
  description = "Function name."
  value       = module.function.function_name
}
