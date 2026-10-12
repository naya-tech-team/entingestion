provider "aws" {
  region = var.region
  # Refuse to run against any other account, whatever credentials are active.
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    # env also scopes the deploy roles' permissions boundary (terraform/bootstrap/access.tf).
    tags = merge(var.tags, local.mandatory_tags, { env = var.env })
  }
}

# Where backups are copied (disaster recovery, backup_enabled). Only the backup module uses it.
provider "aws" {
  alias               = "backup_copy"
  region              = var.backup_copy_region
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = merge(var.tags, local.mandatory_tags, { env = var.env })
  }
}

# The organization's mandatory tags (var.mandatory_tags). A resource that sets its own Name keeps it.
locals {
  mandatory_tags = {
    "Name"                 = local.name_prefix
    "Created By"           = var.mandatory_tags.created_by
    "Created Date"         = var.mandatory_tags.created_date
    "Owner"                = var.mandatory_tags.owner
    "Internal Client Code" = var.mandatory_tags.internal_client_code
    "Client"               = var.mandatory_tags.client
    "Project Name"         = var.mandatory_tags.project_name
    "Division"             = var.mandatory_tags.division
  }
}
