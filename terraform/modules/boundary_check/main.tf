# Plan-time warning if a role misses the workload boundary: the deploy role's CreateRole would
# fail (terraform/bootstrap/access.tf). A check, so the plan still shows everything else.

locals {
  mismatched = [for b in var.boundaries : coalesce(b, "none") if coalesce(b, "none") != coalesce(var.expected, "none")]
}

check "roles_carry_the_workload_boundary" {
  assert {
    condition     = length(local.mismatched) == 0
    error_message = "A role in this stack doesn't carry the workload boundary (${coalesce(var.expected, "none")}); found: ${join(", ", distinct(local.mismatched))}."
  }
}
