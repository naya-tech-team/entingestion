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
    content  = "def handler(event, context):\n    return {'batchItemFailures': []}\n"
  }
}

module "queue" {
  source                     = "../../../sqs_queue"
  name                       = "${var.name}-example"
  visibility_timeout_seconds = 180
}

data "aws_iam_policy_document" "consume" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
    resources = [module.queue.queue_arn]
  }
}

module "function" {
  source      = "../../../lambda_function"
  name        = "${var.name}-example-consumer"
  handler     = "app.handler"
  code        = { zip_path = data.archive_file.code.output_path, source_code_hash = data.archive_file.code.output_base64sha256 }
  policy_json = data.aws_iam_policy_document.consume.json
  kms_key_arn = module.kms.key_arn
}

module "trigger" {
  source          = "../.."
  function_name   = module.function.function_name
  queue_arn       = module.queue.queue_arn
  max_concurrency = 2
}

output "mapping_uuid" {
  description = "Event source mapping UUID."
  value       = module.trigger.mapping_uuid
}
