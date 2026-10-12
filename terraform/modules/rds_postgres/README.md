# rds_postgres

A single RDS for PostgreSQL instance in private subnets, with the same secure defaults as `aurora_postgres`:
- **Credentials:** the master password is generated and kept in Secrets Manager. IAM database authentication is on.
- **Encryption:** storage, the secret, Performance Insights and the exported log group all use the KMS key. `rds.force_ssl = 1`.
- **Logging:** DDL and slow queries are logged.
- **Network:** not publicly accessible; only `allowed_security_group_ids` can reach 5432.

Use it where Aurora doesn't fit. The **AWS free plan** is the main case: it allows Aurora only as "express" clusters, which are outside your VPC and can't be created with Terraform, and it doesn't offer RDS Proxy. Free-plan settings: `db.t4g.micro`, 20 GB, `backup_retention_days = 1`, `performance_insights = false`, `monitoring_interval = 0`.

```hcl
module "db" {
  source                     = "../../modules/rds_postgres"
  name                       = "ingestion-dev"
  database_name              = "ingest"
  vpc_id                     = module.vpc.vpc_id
  subnet_ids                 = module.vpc.private_subnet_ids
  allowed_security_group_ids = { index = module.index_sg.id }
  kms_key_arn                = module.kms.key_arn
  backup_retention_days      = 1
  performance_insights       = false
  monitoring_interval        = 0
  deletion_protection        = false
  skip_final_snapshot        = true
}
```

Clients connect with IAM tokens: grant `rds-db:connect` on `${connect_arn_prefix}<user>`, and `GRANT rds_iam TO <user>` in the database.

| Output | Description |
|---|---|
| `endpoint`, `port` | Connection |
| `instance_id`, `resource_id`, `connect_arn_prefix` | IAM auth wiring |
| `master_username`, `master_secret_arn` | Admin access (migrations) |
| `security_group_id` | The instance's group |
| `settings` | Effective settings, for plan assertions |
