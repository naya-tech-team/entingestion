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

module "queue" {
  source                     = "../.."
  name                       = "${var.name}-example"
  visibility_timeout_seconds = 180
  max_receive_count          = 5
}

output "queue_url" {
  description = "Queue URL."
  value       = module.queue.queue_url
}
