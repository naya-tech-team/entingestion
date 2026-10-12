# backup_plan

AWS Backup for a stack's databases and tables: a vault in this region (your KMS key), a daily plan with a retention, and a copy of every recovery point to a vault in another region (its own KMS key, created here). The backup role carries the workload boundary.

The copy region comes from the `aws.copy` provider:

```hcl
provider "aws" {
  alias  = "backup_copy"
  region = var.backup_copy_region
}

module "backup" {
  source    = "../../modules/backup_plan"
  count     = var.backup_enabled ? 1 : 0
  providers = { aws = aws, aws.copy = aws.backup_copy }

  name                 = "ingestion-prod-backup"
  kms_key_arn          = module.kms.key_arn
  resource_arns        = [module.status_table.arn, module.db[0].cluster_arn]
  permissions_boundary = local.permissions_boundary
}
```

Before the first backup of DynamoDB tables, turn on AWS Backup's advanced features for DynamoDB once per account and region (`aws backup update-region-settings --resource-type-management-preference DynamoDB=true`); without them DynamoDB recovery points can't be copied to another region. Not on the AWS free plan.

| Output | Description |
|---|---|
| `vault_arn`, `copy_vault_arn` | The two vaults |
| `plan_id` | The plan |
| `settings`, `role_permissions_boundaries` | For plan assertions |

A runnable example is in [`examples/basic`](examples/basic).
