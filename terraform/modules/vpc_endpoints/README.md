# vpc_endpoints

VPC endpoints for private subnets with no NAT.

- **Gateway endpoints** (S3, DynamoDB) are free and attach to route tables. The module also outputs their managed prefix list ids, so security groups can allow 443 to them.
- **Interface endpoints** are billed per AZ-hour (about $7 a month per AZ), so list only the services you use. Pass fewer `subnet_ids` to cut cost. A security group allows 443 from `allowed_security_group_ids`.

```hcl
module "endpoints" {
  source                     = "../../modules/vpc_endpoints"
  name                       = "ingestion-dev"
  vpc_id                     = module.vpc.vpc_id
  route_table_ids            = module.vpc.private_route_table_ids
  subnet_ids                 = slice(module.vpc.private_subnet_ids, 0, 2)
  interface_services         = ["secretsmanager"]
  allowed_security_group_ids = { migrate = module.migrate_sg.id }
}
```

| Output | Description |
|---|---|
| `gateway_endpoint_ids`, `interface_endpoint_ids` | Endpoints by service |
| `gateway_prefix_list_ids` | For security group egress to S3 / DynamoDB |
| `security_group_id` | On the interface endpoints |
