# Private VPC (no NAT) for the database and the Lambdas that talk to it (correction 5).

module "vpc" {
  source = "../../modules/vpc"

  name                    = local.name_prefix
  cidr_block              = var.vpc_cidr
  az_count                = var.az_count
  flow_log_retention_days = var.log_retention_days
  kms_key_arn             = module.kms.key_arn
  permissions_boundary    = local.permissions_boundary
}

module "endpoints" {
  source = "../../modules/vpc_endpoints"

  name            = local.name_prefix
  vpc_id          = module.vpc.vpc_id
  route_table_ids = module.vpc.private_route_table_ids
  subnet_ids      = slice(module.vpc.private_subnet_ids, 0, var.interface_endpoint_az_count)
  # migrate reads the master secret; every other in-VPC call uses the free gateway endpoints.
  interface_services         = ["secretsmanager"]
  allowed_security_group_ids = { migrate = module.migrate_sg.id }
  policies = {
    s3       = data.aws_iam_policy_document.endpoint["s3"].json
    dynamodb = data.aws_iam_policy_document.endpoint["dynamodb"].json
  }
}

# The gateway endpoints reach only this account's buckets and tables, so a function in the VPC
# can't move data to another account's. IAM still decides what each function may do. Nothing in
# the VPC reads AWS-owned buckets: Lambda fetches code outside the VPC and the RDS CA bundle is
# in the package.
data "aws_iam_policy_document" "endpoint" {
  for_each = toset(["s3", "dynamodb"])

  statement {
    sid       = "OnlyThisAccountsResources"
    actions   = ["${each.key}:*"]
    resources = ["*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceAccount"
      values   = [local.account_id]
    }
  }
}

# Index (and, later, drift check): PostgreSQL via the proxy; S3 + DynamoDB via gateway endpoints.
module "db_client_sg" {
  source = "../../modules/security_group"

  name        = "${local.name_prefix}-db-client"
  description = "Lambdas that write pgvector (through RDS Proxy, or directly in rds mode)"
  vpc_id      = module.vpc.vpc_id

  egress = {
    db       = { description = "PostgreSQL (RDS Proxy, or the RDS instance)", from_port = 5432, to_port = 5432, referenced_security_group_id = local.db_client_target }
    s3       = { description = "S3 gateway endpoint", from_port = 443, to_port = 443, prefix_list_id = module.endpoints.gateway_prefix_list_ids["s3"] }
    dynamodb = { description = "DynamoDB gateway endpoint", from_port = 443, to_port = 443, prefix_list_id = module.endpoints.gateway_prefix_list_ids["dynamodb"] }
  }
}

# migrate: master user straight to the writer endpoint; secret via the Secrets Manager endpoint.
module "migrate_sg" {
  source = "../../modules/security_group"

  name        = "${local.name_prefix}-migrate"
  description = "migrate Lambda: PostgreSQL writer and Secrets Manager"
  vpc_id      = module.vpc.vpc_id

  egress = {
    db      = { description = "PostgreSQL writer endpoint", from_port = 5432, to_port = 5432, referenced_security_group_id = local.db_migrate_target }
    secrets = { description = "Secrets Manager endpoint", from_port = 443, to_port = 443, referenced_security_group_id = module.endpoints.security_group_id }
  }
}
