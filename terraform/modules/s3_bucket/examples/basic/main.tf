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

data "aws_caller_identity" "current" {}

module "bucket" {
  source        = "../.."
  name          = "${var.name}-example-${data.aws_caller_identity.current.account_id}"
  kms_key_arn   = module.kms.key_arn
  force_destroy = true

  lifecycle_rules = [{
    id                                     = "default"
    abort_incomplete_multipart_upload_days = 7
    noncurrent_version_transitions         = [{ days = 30, storage_class = "GLACIER_IR" }]
  }]
}

output "bucket" {
  description = "Bucket name."
  value       = module.bucket.id
}
