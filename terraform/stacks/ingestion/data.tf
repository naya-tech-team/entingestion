# Producer resources are read-only inputs (brief §8.4), found through the SSM hand-off.
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

data "aws_ssm_parameter" "producer" {
  for_each = toset(["articles_stream_arn", "articles_table_name", "process_table_name", "raw_bucket_name", "kms_key_arn"])
  name     = "${var.producer_ssm_prefix}/${each.value}"
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
  name_prefix = "ingestion-${var.env}"
  is_prod     = var.env == "prod"

  # Every role the stack creates carries the workload boundary (terraform/bootstrap/access.tf);
  # the deploy role may only create roles that do.
  permissions_boundary = var.workload_boundary_enabled ? coalesce(var.permissions_boundary_arn, "arn:${local.partition}:iam::${local.account_id}:policy/entingestion-${var.env}-workload-boundary") : null

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
