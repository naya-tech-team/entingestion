# Disaster recovery (prod; backup_enabled): daily AWS Backup of the database and the status and
# cache tables, kept backup_retention_days and copied to backup_copy_region. On top of PITR on
# the tables and the database's own automated backups. RPO/RTO and restores: docs/RUNBOOK.md
# "Backups and restore". Staging is rebuilt from the producer's raw objects (re-run), so it isn't
# backed up; S3 cross-region replication of raw/staging is a documented follow-up.

module "backup" {
  source    = "../../modules/backup_plan"
  count     = var.backup_enabled ? 1 : 0
  providers = { aws = aws, aws.copy = aws.backup_copy }

  name           = "${local.name_prefix}-backup"
  kms_key_arn    = module.kms.key_arn
  retention_days = var.backup_retention_days
  resource_arns = concat(
    [module.status_table.arn, module.cache_table.arn],
    local.use_aurora ? [one(module.db[*].cluster_arn)] : [one(module.rds[*].arn)],
  )
  permissions_boundary = local.permissions_boundary
}
