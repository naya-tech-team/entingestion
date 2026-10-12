# aurora_postgres

An Aurora PostgreSQL Serverless v2 cluster in private subnets.

- **Credentials:** the master password is generated and kept in Secrets Manager (`manage_master_user_password`), encrypted with your key. IAM database authentication is on.
- **Encryption:** storage, the master secret, Performance Insights and the exported `postgresql` log group all use the KMS key. `rds.force_ssl = 1` is always set.
- **Monitoring:** Performance Insights, and Enhanced Monitoring when `monitoring_interval > 0`.
- **Protection:** automated backups, plus a final snapshot unless `skip_final_snapshot` is set. `deletion_protection` defaults to on, and the cluster has `prevent_destroy` (like `dynamodb_table`), so Terraform never deletes it. `make destroy` in a disposable environment deletes it with the AWS CLI first (terraform/scripts/teardown.py).
- **Network:** only `allowed_security_group_ids` can reach 5432.

```hcl
module "db" {
  source                     = "../../modules/aurora_postgres"
  name                       = "ingestion-dev"
  engine_version             = "16.8"
  database_name              = "ingest"
  vpc_id                     = module.vpc.vpc_id
  subnet_ids                 = module.vpc.private_subnet_ids
  allowed_security_group_ids = { proxy = module.proxy.security_group_id }
  kms_key_arn                = module.kms.key_arn
  min_acu                    = 0.5
  max_acu                    = 4
  deletion_protection        = false
  skip_final_snapshot        = true
}
```

| Output | Description |
|---|---|
| `endpoint`, `reader_endpoint`, `port`, `database_name` | Connection |
| `cluster_id`, `cluster_arn`, `cluster_resource_id` | For RDS Proxy and `rds-db:connect` |
| `master_username`, `master_secret_arn` | Admin access (migrations) |
| `security_group_id` | The cluster's group |
| `settings` | Effective settings, for plan assertions |
