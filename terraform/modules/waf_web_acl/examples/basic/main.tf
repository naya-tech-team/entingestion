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

module "waf" {
  source          = "../.."
  name            = "${var.name}-example"
  log_kms_key_arn = module.kms.key_arn
  rate_limit      = 1000
}

output "web_acl_arn" {
  description = "Web ACL ARN; associate it with an API stage or ALB."
  value       = module.waf.web_acl_arn
}
