# Tagged with the readable name; the actual group name gets a unique suffix.
# Deny by default: AWS's implicit allow-all egress rule is removed when the group is created by
# Terraform with no inline rules, and only the rules passed in are added.
resource "aws_security_group" "this" {
  #checkov:skip=CKV2_AWS_5:Created for the caller to attach (Lambda, endpoint or proxy ENIs).
  # name_prefix, not name: create_before_destroy needs the replacement's name to be free.
  name_prefix = "${var.name}-"
  description = var.description
  vpc_id      = var.vpc_id
  tags        = { Name = var.name }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "this" {
  for_each = var.ingress

  security_group_id            = aws_security_group.this.id
  description                  = each.value.description
  from_port                    = each.value.from_port
  to_port                      = each.value.to_port
  ip_protocol                  = each.value.ip_protocol
  cidr_ipv4                    = each.value.cidr_ipv4
  referenced_security_group_id = each.value.referenced_security_group_id
  prefix_list_id               = each.value.prefix_list_id
}

resource "aws_vpc_security_group_egress_rule" "this" {
  for_each = var.egress

  security_group_id            = aws_security_group.this.id
  description                  = each.value.description
  from_port                    = each.value.from_port
  to_port                      = each.value.to_port
  ip_protocol                  = each.value.ip_protocol
  cidr_ipv4                    = each.value.cidr_ipv4
  referenced_security_group_id = each.value.referenced_security_group_id
  prefix_list_id               = each.value.prefix_list_id
}
