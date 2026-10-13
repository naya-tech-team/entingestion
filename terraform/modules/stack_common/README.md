# stack_common

What every stack derives the same way from its environment, account and tags, in one place. It has no providers or data sources, so a stack can use its outputs in its `provider` blocks.

```hcl
module "common" {
  source = "../../modules/stack_common"

  stack                     = "producer"
  env                       = var.env
  aws_account_id            = var.aws_account_id
  mandatory_tags            = var.mandatory_tags
  tags                      = var.tags
  workload_boundary_enabled = var.workload_boundary_enabled
  permissions_boundary_arn  = var.permissions_boundary_arn
  principals                = { admins = var.admin_principals }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = module.common.default_tags
  }
}
```

| Output | Description |
|---|---|
| `name_prefix` | `<stack>-<env>`, also the default `Name` tag |
| `is_prod` | `env == "prod"` |
| `default_tags` | `merge(tags, mandatory tags, { env })`: the mandatory tags and `env` win over extra tags |
| `mandatory_tags` | The 8 organization tags as AWS tag keys (`Name`, `Created By`, ...) |
| `boundary_policy_name` | `entingestion-<env>-workload-boundary` (created by terraform/bootstrap) |
| `permissions_boundary` | Its ARN in this account, `permissions_boundary_arn` if set, or `null` when `workload_boundary_enabled` is false |
| `iam_arn_prefix` | `arn:<partition>:iam::<account>:` |
| `expanded_principals` | `principals` with `role/<name>` and `user/<name>` turned into ARNs in this account; full ARNs unchanged |

The calling stack keeps the validations of its own `mandatory_tags` variable, so a bad value fails on the stack's input.
