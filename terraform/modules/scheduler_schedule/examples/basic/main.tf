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
  alias       = "${var.name}-schedule-example"
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

module "function" {
  source      = "../../../lambda_function"
  name        = "${var.name}-scheduled"
  handler     = "app.handler"
  code        = { zip_path = data.archive_file.code.output_path, source_code_hash = data.archive_file.code.output_base64sha256 }
  policy_json = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "sts:GetCallerIdentity", Resource = "*" }] })
  kms_key_arn = module.kms.key_arn
}

module "schedule" {
  source              = "../.."
  name                = "${var.name}-hourly"
  description         = "Calls the example function every hour"
  schedule_expression = "rate(1 hour)"
  target_arn          = module.function.function_arn
  kms_key_arn         = module.kms.key_arn
  input               = jsonencode({ source = "example" })
}

output "schedule_arn" {
  description = "Schedule ARN."
  value       = module.schedule.schedule_arn
}
