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
  alias       = "${var.name}-sfn-example"
  description = "Example key"
  policy_statements = [{
    sid        = "CloudWatchLogs"
    actions    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    principals = [{ type = "Service", identifiers = ["logs.${var.region}.amazonaws.com"] }]
  }]
}

module "workflow" {
  source      = "../.."
  name        = "${var.name}-example"
  kms_key_arn = module.kms.key_arn
  definition = jsonencode({
    StartAt = "Wait"
    States = {
      Wait = { Type = "Wait", Seconds = 1, Next = "Done" }
      Done = { Type = "Succeed" }
    }
  })
}

output "state_machine_arn" {
  description = "State machine ARN."
  value       = module.workflow.state_machine_arn
}
