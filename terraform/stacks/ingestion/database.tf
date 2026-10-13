# The vector store: Aurora + RDS Proxy (database_mode = "aurora", the production design) or a
# single RDS instance (database_mode = "rds", for the AWS free plan). Both use IAM auth only:
# no stored passwords for the app users.

locals {
  db_name    = "ingest"
  db_writer  = "ingest_writer"
  db_reader  = "ingest_reader"
  db_master  = "ingest_admin"
  db_ops     = "ingest_ops" # vector-table status and switch (migration 004)
  use_aurora = var.database_mode == "aurora"
}

module "db" {
  source = "../../modules/aurora_postgres"
  count  = local.use_aurora ? 1 : 0

  name                       = local.name_prefix
  engine_version             = var.aurora_engine_version
  database_name              = local.db_name
  master_username            = local.db_master
  instance_count             = var.aurora_instance_count
  min_acu                    = var.aurora_min_acu
  max_acu                    = var.aurora_max_acu
  vpc_id                     = module.vpc.vpc_id
  subnet_ids                 = module.vpc.private_subnet_ids
  allowed_security_group_ids = { migrate = module.migrate_sg.id }
  kms_key_arn                = module.kms.key_arn
  deletion_protection        = local.is_prod
  skip_final_snapshot        = !local.is_prod
  backup_retention_days      = var.db_backup_retention_days
  monitoring_interval        = var.db_monitoring_interval
  log_retention_days         = var.log_retention_days
  permissions_boundary       = local.permissions_boundary
}

module "proxy" {
  source = "../../modules/rds_proxy"
  count  = local.use_aurora ? 1 : 0

  name                       = local.name_prefix
  vpc_id                     = module.vpc.vpc_id
  subnet_ids                 = module.vpc.private_subnet_ids
  cluster_id                 = module.db[0].cluster_id
  cluster_resource_id        = module.db[0].cluster_resource_id
  db_security_group_id       = module.db[0].security_group_id
  allowed_security_group_ids = { db_clients = module.db_client_sg.id }
  permissions_boundary       = local.permissions_boundary
}

module "rds" {
  source = "../../modules/rds_postgres"
  count  = local.use_aurora ? 0 : 1

  name                       = local.name_prefix
  engine_version             = var.rds_engine_version
  instance_class             = var.rds_instance_class
  database_name              = local.db_name
  master_username            = local.db_master
  vpc_id                     = module.vpc.vpc_id
  subnet_ids                 = module.vpc.private_subnet_ids
  allowed_security_group_ids = { db_clients = module.db_client_sg.id, migrate = module.migrate_sg.id }
  kms_key_arn                = module.kms.key_arn
  deletion_protection        = local.is_prod
  skip_final_snapshot        = !local.is_prod
  backup_retention_days      = var.db_backup_retention_days
  performance_insights       = var.db_performance_insights
  monitoring_interval        = var.db_monitoring_interval
  log_retention_days         = var.log_retention_days
  permissions_boundary       = local.permissions_boundary
}

locals {
  # Where each kind of client connects, whichever database mode is active.
  db_host           = local.use_aurora ? one(module.proxy[*].endpoint) : one(module.rds[*].endpoint)
  db_direct_host    = local.use_aurora ? one(module.db[*].endpoint) : one(module.rds[*].endpoint)
  db_master_secret  = local.use_aurora ? one(module.db[*].master_secret_arn) : one(module.rds[*].master_secret_arn)
  db_connect_prefix = local.use_aurora ? one(module.proxy[*].connect_arn_prefix) : one(module.rds[*].connect_arn_prefix)
  db_client_target  = local.use_aurora ? one(module.proxy[*].security_group_id) : one(module.rds[*].security_group_id)
  db_migrate_target = local.use_aurora ? one(module.db[*].security_group_id) : one(module.rds[*].security_group_id)
  # RDS Proxy presents Amazon Trust Services certificates (in the system store); a direct RDS
  # endpoint presents the RDS CA, bundled in the core package.
  db_sslrootcert = local.use_aurora ? "system" : "/var/task/certs/rds-global-bundle.pem"
}
