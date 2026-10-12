# security_group

A security group that allows nothing except the rules you pass in: ingress and egress maps keyed by a stable label, each rule with one source or destination (a CIDR, another security group, or a prefix list). With no `egress`, nothing leaves.

The group is created before its replacement is destroyed, so a rename doesn't detach running ENIs.

```hcl
module "db_clients" {
  source      = "../../modules/security_group"
  name        = "ingestion-dev-db-clients"
  description = "Lambdas that talk to the database"
  vpc_id      = module.vpc.vpc_id
  egress = {
    proxy = { description = "PostgreSQL via RDS Proxy", from_port = 5432, to_port = 5432, referenced_security_group_id = module.proxy.security_group_id }
    s3    = { description = "S3 gateway endpoint", from_port = 443, to_port = 443, prefix_list_id = data.aws_ec2_managed_prefix_list.s3.id }
  }
}
```

| Output | Description |
|---|---|
| `id`, `arn` | The security group |
