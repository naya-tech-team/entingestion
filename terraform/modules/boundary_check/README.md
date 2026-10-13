# boundary_check

The plan-time check that every IAM role a stack creates carries the workload permissions boundary (`check "roles_carry_the_workload_boundary"`). The deploy roles may only create roles that carry it (terraform/bootstrap), so a role without it would fail at apply; the check warns at plan time instead, naming the boundaries it found.

```hcl
module "boundary_check" {
  source = "../../modules/boundary_check"

  # Every module that creates a role reports role_permissions_boundaries; a new one belongs here.
  boundaries = flatten([
    module.api_function.role_permissions_boundaries,
    module.backup[*].role_permissions_boundaries,
  ])
  expected = module.common.permissions_boundary
}
```

| Output | Description |
|---|---|
| `all_bounded` | Every role carries `expected` (null = no boundary) |
| `role_count` | How many roles were checked |
