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

provider "aws" {
  alias  = "copy"
  region = var.copy_region
}

variable "region" {
  description = "Region to deploy the example in."
  type        = string
  default     = "us-west-2"
}

variable "copy_region" {
  description = "Region the backups are copied to."
  type        = string
  default     = "us-east-2"
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
  source      = "../../../dynamodb_table"
  name        = "${var.name}-example"
  hash_key    = "pk"
  attributes  = [{ name = "pk", type = "S" }]
  kms_key_arn = module.kms.key_arn
}

module "backup" {
  source    = "../.."
  providers = { aws = aws, aws.copy = aws.copy }

  name          = "${var.name}-example-backup"
  kms_key_arn   = module.kms.key_arn
  resource_arns = [module.table.arn]
}

output "vault_arn" {
  description = "Vault in this region."
  value       = module.backup.vault_arn
}
