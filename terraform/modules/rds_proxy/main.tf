data "aws_partition" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

resource "aws_security_group" "this" {
  #checkov:skip=CKV2_AWS_5:Created for the caller to attach (Lambda, endpoint or proxy ENIs).
  name_prefix = "${var.name}-proxy-"
  description = "RDS Proxy ${var.name}: 5432 from allowed clients, 5432 to the cluster"
  vpc_id      = var.vpc_id
  tags        = { Name = "${var.name}-proxy" }

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

resource "aws_vpc_security_group_egress_rule" "db" {
  security_group_id            = aws_security_group.this.id
  description                  = "PostgreSQL to the cluster"
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  referenced_security_group_id = var.db_security_group_id
}

resource "aws_vpc_security_group_ingress_rule" "db_from_proxy" {
  security_group_id            = var.db_security_group_id
  description                  = "PostgreSQL from RDS Proxy ${var.name}"
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.this.id
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["rds.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "this" {
  name                 = "${var.name}-rds-proxy"
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  permissions_boundary = var.permissions_boundary
}

# End-to-end IAM: the proxy reaches the cluster with IAM tokens too, so no database passwords
# are stored for application users (each needs `GRANT rds_iam`).
data "aws_iam_policy_document" "connect" {
  statement {
    actions = ["rds-db:connect"]
    resources = [
      "arn:${data.aws_partition.current.partition}:rds-db:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:dbuser:${var.cluster_resource_id}/*",
    ]
  }
}

resource "aws_iam_role_policy" "connect" {
  name   = "connect-to-cluster"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.connect.json
}

resource "aws_db_proxy" "this" {
  name                   = var.name
  engine_family          = "POSTGRESQL"
  role_arn               = aws_iam_role.this.arn
  vpc_subnet_ids         = var.subnet_ids
  vpc_security_group_ids = [aws_security_group.this.id]
  require_tls            = true
  default_auth_scheme    = "IAM_AUTH"
  idle_client_timeout    = var.idle_client_timeout
  debug_logging          = false

  depends_on = [aws_iam_role_policy.connect]
}

resource "aws_db_proxy_default_target_group" "this" {
  db_proxy_name = aws_db_proxy.this.name

  connection_pool_config {
    max_connections_percent   = var.max_connections_percent
    connection_borrow_timeout = 120
  }
}

resource "aws_db_proxy_target" "this" {
  db_proxy_name         = aws_db_proxy.this.name
  target_group_name     = aws_db_proxy_default_target_group.this.name
  db_cluster_identifier = var.cluster_id
}
