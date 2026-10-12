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

variable "vpc_id" {
  description = "VPC for the endpoints."
  type        = string
}

variable "route_table_ids" {
  description = "Private route tables."
  type        = list(string)
}

module "endpoints" {
  source          = "../.."
  name            = "example"
  vpc_id          = var.vpc_id
  route_table_ids = var.route_table_ids
}

output "prefix_lists" {
  description = "S3 and DynamoDB prefix lists for security group rules."
  value       = module.endpoints.gateway_prefix_list_ids
}
