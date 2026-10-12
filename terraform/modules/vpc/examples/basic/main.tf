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
  alias       = "${var.name}-vpc-example"
  description = "Example key"
  policy_statements = [{
    sid        = "CloudWatchLogs"
    actions    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    principals = [{ type = "Service", identifiers = ["logs.${var.region}.amazonaws.com"] }]
  }]
}

module "vpc" {
  source      = "../.."
  name        = "${var.name}-example"
  az_count    = 2
  kms_key_arn = module.kms.key_arn
}

output "private_subnet_ids" {
  description = "Private subnets."
  value       = module.vpc.private_subnet_ids
}
