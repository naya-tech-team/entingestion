terraform {
  required_version = ">= 1.10"
}

variable "boundary_arn" {
  description = "The workload boundary every role must carry."
  type        = string
  default     = "arn:aws:iam::123456789012:policy/entingestion-dev-workload-boundary"
}

module "boundary_check" {
  source = "../.."

  # In a stack: flatten([module.fn.role_permissions_boundaries, ...]).
  boundaries = [var.boundary_arn]
  expected   = var.boundary_arn
}

output "all_bounded" {
  description = "Whether every role carries the boundary."
  value       = module.boundary_check.all_bounded
}
