terraform {
  required_version = ">= 1.10"
}

variable "aws_account_id" {
  description = "Account the example stack deploys to."
  type        = string
  default     = "123456789012"
}

module "common" {
  source = "../.."

  stack          = "example"
  env            = "dev"
  aws_account_id = var.aws_account_id
  mandatory_tags = {
    created_by           = "example"
    created_date         = "2026-01-01"
    owner                = "example"
    internal_client_code = "example"
    client               = "example"
    project_name         = "example"
    division             = "example"
  }
  principals = { writers = ["role/example-writer"] }
}

output "default_tags" {
  description = "What the stack's provider would tag every resource with."
  value       = module.common.default_tags
}

output "writers" {
  description = "The writers as ARNs."
  value       = module.common.expanded_principals["writers"]
}
