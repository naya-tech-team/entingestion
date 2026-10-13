# Producer resources are read-only inputs, found through the SSM hand-off
# (<ssm_prefix>/<env>/producer/*, published by the producer stack).
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

data "aws_ssm_parameter" "producer" {
  for_each = toset(["articles_stream_arn", "articles_table_name", "process_table_name", "raw_bucket_name", "kms_key_arn"])
  name     = "${local.producer_ssm_prefix}/${each.value}"
}

data "aws_dynamodb_table" "producer_articles" {
  name = data.aws_ssm_parameter.producer["articles_table_name"].value
}

data "aws_dynamodb_table" "producer_process" {
  name = data.aws_ssm_parameter.producer["process_table_name"].value
}

data "aws_s3_bucket" "producer_raw" {
  bucket = data.aws_ssm_parameter.producer["raw_bucket_name"].value
}

locals {
  account_id  = data.aws_caller_identity.current.account_id
  partition   = data.aws_partition.current.partition
  region      = data.aws_region.current.region
  name_prefix = module.common.name_prefix
  is_prod     = module.common.is_prod

  # Every role the stack creates carries the workload boundary (terraform/bootstrap/access.tf);
  # the deploy role may only create roles that do.
  permissions_boundary = module.common.permissions_boundary

  # Where the producer stack of this environment publishes its hand-off values.
  producer_ssm_prefix = coalesce(var.producer_ssm_prefix, "${var.ssm_prefix}/${var.env}/producer")

  producer = {
    articles_table_arn  = data.aws_dynamodb_table.producer_articles.arn
    articles_table_name = data.aws_dynamodb_table.producer_articles.name
    articles_stream_arn = data.aws_ssm_parameter.producer["articles_stream_arn"].value
    process_table_arn   = data.aws_dynamodb_table.producer_process.arn
    process_table_name  = data.aws_dynamodb_table.producer_process.name
    raw_bucket_arn      = data.aws_s3_bucket.producer_raw.arn
    kms_key_arn         = data.aws_ssm_parameter.producer["kms_key_arn"].value
  }
}
