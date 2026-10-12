# Disaster recovery (prod; backup_enabled): daily AWS Backup of the tables the pipeline depends
# on, kept backup_retention_days and copied to backup_copy_region, on top of their PITR. The
# idempotency table is short-lived and not backed up. The raw bucket is versioned; S3
# cross-region replication is a documented follow-up (docs/RUNBOOK.md "Backups and restore").

module "backup" {
  source    = "../../modules/backup_plan"
  count     = var.backup_enabled ? 1 : 0
  providers = { aws = aws, aws.copy = aws.backup_copy }

  name                 = "${local.name_prefix}-backup"
  kms_key_arn          = module.kms.key_arn
  retention_days       = var.backup_retention_days
  resource_arns        = [module.process_table.arn, module.articles_table.arn, module.uploads_table.arn]
  permissions_boundary = local.permissions_boundary
}
