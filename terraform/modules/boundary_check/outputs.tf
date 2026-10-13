output "all_bounded" {
  description = "Whether every role carries the expected boundary."
  value       = length(local.mismatched) == 0
}

output "role_count" {
  description = "How many roles were checked."
  value       = length(var.boundaries)
}
