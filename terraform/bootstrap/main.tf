provider "aws" {
  region              = var.region
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = local.mandatory_tags
  }
}

# The organization's mandatory tags (var.mandatory_tags). A resource that sets its own Name keeps it.
locals {
  mandatory_tags = {
    "Name"                 = "entingestion-bootstrap"
    "Created By"           = var.mandatory_tags.created_by
    "Created Date"         = var.mandatory_tags.created_date
    "Owner"                = var.mandatory_tags.owner
    "Internal Client Code" = var.mandatory_tags.internal_client_code
    "Client"               = var.mandatory_tags.client
    "Project Name"         = var.mandatory_tags.project_name
    "Division"             = var.mandatory_tags.division
  }
}

module "kms" {
  source = "../modules/kms_key"

  alias       = "entingestion-tfstate"
  description = "Encrypts the entingestion Terraform state bucket."
}

module "state_bucket" {
  source = "../modules/s3_bucket"

  name             = "entingestion-tfstate-${var.aws_account_id}-${var.region}"
  kms_key_arn      = module.kms.key_arn
  versioning       = true
  policy_documents = [data.aws_iam_policy_document.state_access.json] # access.tf

  lifecycle_rules = [{
    id                                     = "expire-old-state-versions"
    abort_incomplete_multipart_upload_days = 7
    noncurrent_version_expiration_days     = 90
  }]
}
