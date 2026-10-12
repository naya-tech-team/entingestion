output "vpc_id" {
  description = "VPC id."
  value       = aws_vpc.this.id
}

output "cidr_block" {
  description = "VPC CIDR."
  value       = aws_vpc.this.cidr_block
}

output "private_subnet_ids" {
  description = "Private subnet ids, one per AZ."
  value       = aws_subnet.private[*].id
}

output "private_route_table_ids" {
  description = "Private route table ids (for gateway endpoints)."
  value       = aws_route_table.private[*].id
}

output "azs" {
  description = "Availability Zones used."
  value       = local.azs
}

output "role_permissions_boundaries" {
  description = "Permissions boundary of each IAM role the module created, for plan assertions in consuming stacks."
  value       = [aws_iam_role.flow.permissions_boundary]
}
