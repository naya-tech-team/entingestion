provider "aws" {
  region = var.region
  # Refuse to run against any other account, whatever credentials are active.
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    # The mandatory tags (Name falls back to the stack) and env, which also scopes the deploy
    # roles' permissions boundary (terraform/bootstrap/access.tf).
    tags = module.common.default_tags
  }
}

# Where backups are copied (disaster recovery, backup_enabled). Only the backup module uses it;
# without a backup_copy_region (no backups) it points at the stack's own region, unused.
provider "aws" {
  alias               = "backup_copy"
  region              = coalesce(var.backup_copy_region, var.region)
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = module.common.default_tags
  }
}

# Name prefix, tags and workload boundary, derived like every other stack's.
module "common" {
  source = "../../modules/stack_common"

  stack                     = "ingestion"
  env                       = var.env
  aws_account_id            = var.aws_account_id
  partition                 = data.aws_partition.current.partition
  mandatory_tags            = var.mandatory_tags
  tags                      = var.tags
  workload_boundary_enabled = var.workload_boundary_enabled
  permissions_boundary_arn  = var.permissions_boundary_arn
}
