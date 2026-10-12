output "gateway_endpoint_ids" {
  description = "Gateway endpoint ids by service."
  value       = { for k, e in aws_vpc_endpoint.gateway : k => e.id }
}

output "interface_endpoint_ids" {
  description = "Interface endpoint ids by service."
  value       = { for k, e in aws_vpc_endpoint.interface : k => e.id }
}

output "gateway_prefix_list_ids" {
  description = "Managed prefix list ids by gateway service, for security group egress (e.g. 443 to S3)."
  value       = { for k, p in data.aws_ec2_managed_prefix_list.gateway : k => p.id }
}

output "security_group_id" {
  description = "Security group on the interface endpoints, or null when there are none."
  value       = one(aws_security_group.interface[*].id)
}
