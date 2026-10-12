data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}

locals {
  family = "aurora-postgresql${split(".", var.engine_version)[0]}"
  parameters = merge(
    {
      "rds.force_ssl"              = { value = "1", apply_method = "immediate" }
      "log_statement"              = { value = "ddl", apply_method = "immediate" }  # schema changes, never data
      "log_min_duration_statement" = { value = "1000", apply_method = "immediate" } # slow queries (ms)
    },
    var.parameters,
  )
}

# Name prefixes: replacements never collide with the old resource's name.
resource "aws_db_subnet_group" "this" {
  name_prefix = "${var.name}-"
  subnet_ids  = var.subnet_ids

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "this" {
  name_prefix = "${var.name}-db-"
  description = "Aurora PostgreSQL ${var.name}: 5432 from allowed clients only"
  vpc_id      = var.vpc_id
  tags        = { Name = "${var.name}-db" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "clients" {
  for_each = var.allowed_security_group_ids

  security_group_id            = aws_security_group.this.id
  description                  = "PostgreSQL from ${each.key}"
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  referenced_security_group_id = each.value
}

resource "aws_rds_cluster_parameter_group" "this" {
  name_prefix = "${var.name}-"
  family      = local.family

  dynamic "parameter" {
    for_each = local.parameters

    content {
      name         = parameter.key
      value        = parameter.value.value
      apply_method = parameter.value.apply_method
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

# Pre-created so the exported logs get retention and KMS.
resource "aws_cloudwatch_log_group" "postgresql" {
  #checkov:skip=CKV_AWS_338:Retention is set by the caller per environment.
  name              = "/aws/rds/cluster/${var.name}/postgresql"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

resource "aws_rds_cluster" "this" {
  #checkov:skip=CKV_AWS_139:Deletion protection is set per environment by the caller (on in prod).
  #checkov:skip=CKV2_AWS_27:DDL and slow-query (>1 s) logging are on; logging every statement would write document text to CloudWatch.
  #checkov:skip=CKV2_AWS_8:Backups are AWS Backup's job at the account level; automated backups + PITR are on here.
  cluster_identifier = var.name
  engine             = "aurora-postgresql"
  engine_mode        = "provisioned"
  engine_version     = var.engine_version
  database_name      = var.database_name
  master_username    = var.master_username

  manage_master_user_password   = true
  master_user_secret_kms_key_id = var.kms_key_arn

  iam_database_authentication_enabled = true
  storage_encrypted                   = true
  kms_key_id                          = var.kms_key_arn

  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [aws_security_group.this.id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.this.name

  backup_retention_period         = var.backup_retention_days
  copy_tags_to_snapshot           = true
  deletion_protection             = var.deletion_protection
  skip_final_snapshot             = var.skip_final_snapshot
  final_snapshot_identifier       = var.skip_final_snapshot ? null : "${var.name}-final"
  enabled_cloudwatch_logs_exports = ["postgresql"]

  serverlessv2_scaling_configuration {
    min_capacity = var.min_acu
    max_capacity = var.max_acu
  }

  depends_on = [aws_cloudwatch_log_group.postgresql]

  lifecycle {
    # The cluster holds the vectors. Like dynamodb_table this can't be conditional; deletion
    # protection (per environment) also guards it outside Terraform. To tear down a disposable
    # aurora-mode environment, see docs/RUNBOOK.md.
    prevent_destroy = true
  }
}

data "aws_iam_policy_document" "monitoring_assume" {
  count = var.monitoring_interval > 0 ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "monitoring" {
  count                = var.monitoring_interval > 0 ? 1 : 0
  name                 = "${var.name}-rds-monitoring"
  assume_role_policy   = data.aws_iam_policy_document.monitoring_assume[0].json
  permissions_boundary = var.permissions_boundary
}

resource "aws_iam_role_policy_attachment" "monitoring" {
  count      = var.monitoring_interval > 0 ? 1 : 0
  role       = aws_iam_role.monitoring[0].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

resource "aws_rds_cluster_instance" "this" {
  count = var.instance_count

  identifier                 = "${var.name}-${count.index + 1}"
  cluster_identifier         = aws_rds_cluster.this.id
  instance_class             = "db.serverless"
  engine                     = aws_rds_cluster.this.engine
  engine_version             = aws_rds_cluster.this.engine_version
  db_subnet_group_name       = aws_db_subnet_group.this.name
  auto_minor_version_upgrade = true
  copy_tags_to_snapshot      = true
  ca_cert_identifier         = "rds-ca-rsa2048-g1"

  performance_insights_enabled          = true
  performance_insights_kms_key_id       = var.kms_key_arn
  performance_insights_retention_period = 7

  monitoring_interval = var.monitoring_interval
  monitoring_role_arn = var.monitoring_interval > 0 ? aws_iam_role.monitoring[0].arn : null
}
