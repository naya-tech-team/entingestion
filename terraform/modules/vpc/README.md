# vpc

A private-only VPC. It has one private subnet and route table per AZ and **no internet gateway or NAT**, so nothing in it can reach the internet. AWS services are reached through `vpc_endpoints` (gateway endpoints for S3 and DynamoDB are free).

It also turns on flow logs for all traffic, into a KMS-encrypted log group, and locks the default security group so it has no rules.

```hcl
module "vpc" {
  source      = "../../modules/vpc"
  name        = "ingestion-dev"
  cidr_block  = "10.40.0.0/16"
  az_count    = 3
  kms_key_arn = module.kms.key_arn
}
```

| Output | Description |
|---|---|
| `vpc_id`, `cidr_block`, `azs` | The VPC |
| `private_subnet_ids` | One per AZ |
| `private_route_table_ids` | For gateway endpoints |
