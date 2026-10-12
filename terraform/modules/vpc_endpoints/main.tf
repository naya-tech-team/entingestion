data "aws_region" "current" {}

locals {
  service_prefix = "com.amazonaws.${data.aws_region.current.region}"
}

resource "aws_vpc_endpoint" "gateway" {
  for_each = toset(var.gateway_services)

  vpc_id            = var.vpc_id
  service_name      = "${local.service_prefix}.${each.value}"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = var.route_table_ids
  policy            = lookup(var.policies, each.value, null)

  tags = { Name = "${var.name}-${each.value}" }
}

resource "aws_security_group" "interface" {
  #checkov:skip=CKV2_AWS_5:Created for the caller to attach (Lambda, endpoint or proxy ENIs).
  count = length(var.interface_services) > 0 ? 1 : 0

  name_prefix = "${var.name}-endpoints-"
  description = "HTTPS from allowed clients to interface VPC endpoints"
  vpc_id      = var.vpc_id
  tags        = { Name = "${var.name}-endpoints" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "interface" {
  for_each = length(var.interface_services) > 0 ? var.allowed_security_group_ids : {}

  security_group_id            = aws_security_group.interface[0].id
  description                  = "HTTPS from ${each.key}"
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  referenced_security_group_id = each.value
}

resource "aws_vpc_endpoint" "interface" {
  for_each = toset(var.interface_services)

  vpc_id              = var.vpc_id
  service_name        = "${local.service_prefix}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.subnet_ids
  security_group_ids  = [aws_security_group.interface[0].id]
  private_dns_enabled = true
  policy              = lookup(var.policies, each.value, null)

  tags = { Name = "${var.name}-${each.value}" }
}

# Prefix lists of the gateway services, for security group egress rules.
data "aws_ec2_managed_prefix_list" "gateway" {
  for_each = toset(var.gateway_services)
  name     = "${local.service_prefix}.${each.value}"
}
