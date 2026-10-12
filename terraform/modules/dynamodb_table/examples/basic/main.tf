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

module "table" {
  source              = "../.."
  name                = "${var.name}-example"
  hash_key            = "pk"
  range_key           = "sk"
  attributes          = [{ name = "pk", type = "S" }, { name = "sk", type = "S" }, { name = "status", type = "S" }]
  kms_key_arn         = module.kms.key_arn
  deletion_protection = false
  ttl_attribute       = "expires_at_epoch"
  stream_view_type    = "NEW_AND_OLD_IMAGES"

  global_secondary_indexes = [
    { name = "gsi-status", hash_key = "status", range_key = "sk" },
  ]
}

output "table" {
  description = "Table name."
  value       = module.table.name
}
