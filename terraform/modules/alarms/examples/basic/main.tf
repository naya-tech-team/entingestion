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
  alias       = "${var.name}-example"
  description = "Example key"
}

module "queue" {
  source = "../../../sqs_queue"
  name   = "${var.name}-example"
}

module "alarms" {
  source      = "../.."
  topic_name  = "${var.name}-example-alarms"
  kms_key_arn = module.kms.key_arn

  alarms = {
    dlq-not-empty = {
      description = "Messages in the example DLQ"
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesVisible"
      dimensions  = { QueueName = module.queue.dlq_name }
      statistic   = "Maximum"
      threshold   = 1
    }
  }
}

output "topic_arn" {
  description = "Alarm topic."
  value       = module.alarms.topic_arn
}
