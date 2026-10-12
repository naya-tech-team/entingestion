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
  alias       = "${var.name}-proxy-example"
  description = "Example key"
  policy_statements = [{
    sid        = "CloudWatchLogs"
    actions    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    principals = [{ type = "Service", identifiers = ["logs.${var.region}.amazonaws.com"] }]
  }]
}

module "vpc" {
  source      = "../../../vpc"
  name        = "${var.name}-proxy-example"
  az_count    = 2
  kms_key_arn = module.kms.key_arn
}

module "db" {
  #checkov:skip=CKV_AWS_139:Examples must be destroyable; real stacks keep deletion_protection on.
  source              = "../../../aurora_postgres"
  name                = "${var.name}-proxy-example"
  database_name       = "app"
  vpc_id              = module.vpc.vpc_id
  subnet_ids          = module.vpc.private_subnet_ids
  kms_key_arn         = module.kms.key_arn
  deletion_protection = false
  skip_final_snapshot = true
}

module "proxy" {
  source               = "../.."
  name                 = "${var.name}-proxy-example"
  vpc_id               = module.vpc.vpc_id
  subnet_ids           = module.vpc.private_subnet_ids
  cluster_id           = module.db.cluster_id
  cluster_resource_id  = module.db.cluster_resource_id
  db_security_group_id = module.db.security_group_id
}

output "proxy_endpoint" {
  description = "Proxy endpoint."
  value       = module.proxy.endpoint
}
