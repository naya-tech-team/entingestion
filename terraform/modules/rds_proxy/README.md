# rds_proxy

An RDS Proxy in front of an Aurora PostgreSQL cluster, with **end-to-end IAM auth** (`default_auth_scheme = "IAM_AUTH"`).

Clients connect with an IAM token over TLS, and the proxy reaches the cluster with IAM too. So no database passwords are stored for application users. Each user needs `GRANT rds_iam` in the database, and the client role needs `rds-db:connect` on `${connect_arn_prefix}<user>`.

The module creates the proxy's security group: 5432 in from `allowed_security_group_ids`, and 5432 out to the cluster. It also adds the matching ingress rule to the cluster's group.

```hcl
module "proxy" {
  source                     = "../../modules/rds_proxy"
  name                       = "ingestion-dev"
  vpc_id                     = module.vpc.vpc_id
  subnet_ids                 = module.vpc.private_subnet_ids
  cluster_id                 = module.db.cluster_id
  cluster_resource_id        = module.db.cluster_resource_id
  db_security_group_id       = module.db.security_group_id
  allowed_security_group_ids = { index = module.index_sg.id }
}
```

| Output | Description |
|---|---|
| `endpoint` | Connect here |
| `proxy_arn`, `proxy_resource_id` | The proxy |
| `connect_arn_prefix` | Client `rds-db:connect` ARN prefix |
| `security_group_id` | The proxy's group |
| `settings` | Effective settings, for plan assertions |
