# No providers: the module is pure.

variables {
  stack          = "producer"
  env            = "dev"
  aws_account_id = "123456789012"
  mandatory_tags = {
    created_by           = "alice"
    created_date         = "2026-01-01"
    owner                = "team"
    internal_client_code = "icc"
    client               = "client"
    project_name         = "proj"
    division             = "div"
  }
}

run "names_and_tags" {
  command = plan

  variables {
    tags = { cost_center = "42", Name = "overridden" }
  }

  assert {
    condition     = output.name_prefix == "producer-dev" && !output.is_prod
    error_message = "name_prefix is <stack>-<env>; dev isn't prod."
  }

  assert {
    condition = output.default_tags == {
      "Name"                 = "producer-dev"
      "Created By"           = "alice"
      "Created Date"         = "2026-01-01"
      "Owner"                = "team"
      "Internal Client Code" = "icc"
      "Client"               = "client"
      "Project Name"         = "proj"
      "Division"             = "div"
      "env"                  = "dev"
      "cost_center"          = "42"
    }
    error_message = "default_tags = extra tags, then the mandatory tags (Name = name_prefix) and env, which win."
  }

  assert {
    condition     = length(output.mandatory_tags) == 8 && !contains(keys(output.mandatory_tags), "env")
    error_message = "mandatory_tags are the 8 organization tags."
  }
}

run "prod_is_prod" {
  command = plan

  variables {
    env = "prod"
  }

  assert {
    condition     = output.is_prod && output.boundary_policy_name == "entingestion-prod-workload-boundary"
    error_message = "prod is_prod, with its own boundary."
  }
}

run "boundary_defaults_to_the_environments_workload_boundary" {
  command = plan

  assert {
    condition     = output.permissions_boundary == "arn:aws:iam::123456789012:policy/entingestion-dev-workload-boundary"
    error_message = "The default boundary is this account's entingestion-<env>-workload-boundary."
  }
}

run "boundary_override_partition_and_off" {
  command = plan

  variables {
    partition                = "aws-us-gov"
    permissions_boundary_arn = "arn:aws-us-gov:iam::123456789012:policy/custom"
  }

  assert {
    condition     = output.permissions_boundary == "arn:aws-us-gov:iam::123456789012:policy/custom" && output.iam_arn_prefix == "arn:aws-us-gov:iam::123456789012:"
    error_message = "permissions_boundary_arn replaces the default; the partition shapes the ARN prefix."
  }
}

run "boundary_can_be_turned_off" {
  command = plan

  variables {
    workload_boundary_enabled = false
  }

  assert {
    condition     = output.permissions_boundary == null
    error_message = "workload_boundary_enabled = false means no boundary."
  }
}

run "principals_relative_to_the_account_get_its_arn" {
  command = plan

  variables {
    principals = {
      admins  = ["role/deploy", "arn:aws:iam::210987654321:role/partner"]
      readers = ["user/analyst", "role/search-*"]
      none    = []
    }
  }

  assert {
    condition = output.expanded_principals == {
      admins  = ["arn:aws:iam::123456789012:role/deploy", "arn:aws:iam::210987654321:role/partner"]
      readers = ["arn:aws:iam::123456789012:user/analyst", "arn:aws:iam::123456789012:role/search-*"]
      none    = []
    }
    error_message = "Relative principals get this account's ARN; full ARNs are kept; order is kept."
  }
}

run "rejects_bad_inputs" {
  command = plan

  variables {
    env            = "Prod"
    aws_account_id = "12345"
  }

  expect_failures = [var.env, var.aws_account_id]
}
