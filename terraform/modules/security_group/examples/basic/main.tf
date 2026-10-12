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
  description = "VPC to create the group in."
  type        = string
}

module "app" {
  source      = "../.."
  name        = "example-app"
  description = "Example application tier"
  vpc_id      = var.vpc_id

  egress = {
    https = { description = "HTTPS within the VPC", from_port = 443, to_port = 443, cidr_ipv4 = "10.0.0.0/8" }
  }
}

output "security_group_id" {
  description = "Security group id."
  value       = module.app.id
}
