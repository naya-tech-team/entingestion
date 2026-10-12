terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
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
  alias       = "${var.name}-pipe-example"
  description = "Example key"
  policy_statements = [{
    sid        = "CloudWatchLogs"
    actions    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    principals = [{ type = "Service", identifiers = ["logs.${var.region}.amazonaws.com"] }]
  }]
}

module "table" {
  source              = "../../../dynamodb_table"
  name                = "${var.name}-pipe-example"
  hash_key            = "id"
  attributes          = [{ name = "id", type = "S" }]
  kms_key_arn         = module.kms.key_arn
  stream_view_type    = "NEW_AND_OLD_IMAGES"
  deletion_protection = false
}

module "queue" {
  source = "../../../sqs_queue"
  name   = "${var.name}-pipe-example"
}

data "aws_iam_policy_document" "decrypt" {
  statement {
    actions   = ["kms:Decrypt"]
    resources = [module.kms.key_arn]
  }
}

module "pipe" {
  source            = "../.."
  name              = "${var.name}-example"
  source_stream_arn = module.table.stream_arn
  target_queue_arn  = module.queue.queue_arn
  dlq_arn           = module.queue.dlq_arn
  kms_key_arn       = module.kms.key_arn
  filter_patterns   = [jsonencode({ eventName = ["INSERT", "MODIFY"] })]
  policy_documents  = [data.aws_iam_policy_document.decrypt.json]
}

output "pipe_arn" {
  description = "Pipe ARN."
  value       = module.pipe.pipe_arn
}
