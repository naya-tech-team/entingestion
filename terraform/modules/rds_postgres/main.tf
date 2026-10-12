data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}

locals {
  family = "postgres${split(".", var.engine_version)[0]}"
  parameters = merge(
    {
      "rds.force_ssl"              = { value = "1", apply_method = "pending-reboot" } # static on RDS
      "log_statement"              = { value = "ddl", apply_method = "immediate" }    # schema changes, never data
      "log_min_duration_statement" = { value = "1000", apply_method = "immediate" }   # slow queries (ms)
    },
    var.parameters,
  )
}

# Name prefixes everywhere: replacements (and switching between database modules that share a
# base name) never collide with the old resource's name.
resource "aws_db_subnet_group" "this" {
  name_prefix = "${var.name}-"
  subnet_ids  = var.subnet_ids

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "this" {
  #checkov:skip=CKV2_AWS_5:Attached to the DB instance below.
  name_prefix = "${var.name}-db-"
  description = "RDS PostgreSQL ${var.name}: 5432 from allowed clients only"
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

resource "aws_db_parameter_group" "this" {
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

resource "aws_cloudwatch_log_group" "postgresql" {
  #checkov:skip=CKV_AWS_338:Retention is set by the caller per environment.
  name              = "/aws/rds/instance/${var.name}/postgresql"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
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

resource "aws_db_instance" "this" {
  #checkov:skip=CKV2_AWS_69:rds.force_ssl=1 is always set in the parameter group (a dynamic block checkov can't read).
  #checkov:skip=CKV2_AWS_30:DDL and slow-query logging are on; logging every statement would write document text to CloudWatch.
  #checkov:skip=CKV_AWS_157:Multi-AZ is a per-environment cost choice (multi_az).
  #checkov:skip=CKV_AWS_118:Enhanced Monitoring is configurable (monitoring_interval); off on the free plan.
  #checkov:skip=CKV_AWS_353:Performance Insights is configurable (performance_insights).
  #checkov:skip=CKV_AWS_293:Deletion protection is configurable; disposable environments turn it off.
  #checkov:skip=CKV2_AWS_60:Copy tags to snapshots is on.
  identifier     = var.name
  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class
  db_name        = var.database_name
  username       = var.master_username

  manage_master_user_password   = true
  master_user_secret_kms_key_id = var.kms_key_arn

  iam_database_authentication_enabled = true
  storage_encrypted                   = true
  kms_key_id                          = var.kms_key_arn
  storage_type                        = "gp3"
  allocated_storage                   = var.allocated_storage_gb
  max_allocated_storage               = var.max_allocated_storage_gb > var.allocated_storage_gb ? var.max_allocated_storage_gb : null

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.this.id]
  parameter_group_name   = aws_db_parameter_group.this.name
  publicly_accessible    = false
  multi_az               = var.multi_az
  ca_cert_identifier     = "rds-ca-rsa2048-g1"

  backup_retention_period         = var.backup_retention_days
  copy_tags_to_snapshot           = true
  deletion_protection             = var.deletion_protection
  skip_final_snapshot             = var.skip_final_snapshot
  final_snapshot_identifier       = var.skip_final_snapshot ? null : "${var.name}-final"
  auto_minor_version_upgrade      = true
  enabled_cloudwatch_logs_exports = ["postgresql"]

  performance_insights_enabled          = var.performance_insights
  performance_insights_kms_key_id       = var.performance_insights ? var.kms_key_arn : null
  performance_insights_retention_period = var.performance_insights ? 7 : null

  monitoring_interval = var.monitoring_interval
  monitoring_role_arn = var.monitoring_interval > 0 ? aws_iam_role.monitoring[0].arn : null

  depends_on = [aws_cloudwatch_log_group.postgresql]
}
