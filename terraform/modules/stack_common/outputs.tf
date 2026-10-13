output "name_prefix" {
  description = "<stack>-<env>: the prefix of the stack's resource names, and its Name tag."
  value       = local.name_prefix
}

output "is_prod" {
  description = "Whether env is prod (deletion protection, no force_destroy)."
  value       = var.env == "prod"
}

output "mandatory_tags" {
  description = "The mandatory tags as AWS tag keys (Name, Created By, ...), without env or extra tags."
  value       = local.mandatory_tags
}

output "default_tags" {
  description = "The provider's default_tags: extra tags, the mandatory tags, and env."
  value       = merge(var.tags, local.mandatory_tags, { env = var.env })
}

output "boundary_policy_name" {
  description = "Name of the environment's workload boundary policy (terraform/bootstrap)."
  value       = local.boundary_policy_name
}

output "permissions_boundary" {
  description = "Boundary ARN every role the stack creates carries, or null when workload_boundary_enabled is false."
  value       = var.workload_boundary_enabled ? coalesce(var.permissions_boundary_arn, "${local.iam_arn_prefix}policy/${local.boundary_policy_name}") : null
}

output "iam_arn_prefix" {
  description = "arn:<partition>:iam::<account>: (relative principals are appended to it)."
  value       = local.iam_arn_prefix
}

output "expanded_principals" {
  description = "principals with every relative entry turned into an ARN in this account, same keys and order."
  value = {
    for key, list in var.principals : key => [for p in list : startswith(p, "arn:") ? p : "${local.iam_arn_prefix}${p}"]
  }
}
