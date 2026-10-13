# No providers: the module only compares strings.

run "every_role_bounded" {
  command = plan

  variables {
    boundaries = ["arn:aws:iam::123456789012:policy/b", "arn:aws:iam::123456789012:policy/b"]
    expected   = "arn:aws:iam::123456789012:policy/b"
  }

  assert {
    condition     = output.all_bounded && output.role_count == 2
    error_message = "Roles that carry the expected boundary pass."
  }
}

run "no_boundary_expected_and_none_set" {
  command = plan

  variables {
    boundaries = [null, null]
    expected   = null
  }

  assert {
    condition     = output.all_bounded
    error_message = "With the boundary off, unbounded roles pass."
  }
}

run "a_role_without_the_boundary_is_reported" {
  command = plan

  variables {
    boundaries = ["arn:aws:iam::123456789012:policy/b", null]
    expected   = "arn:aws:iam::123456789012:policy/b"
  }

  assert {
    condition     = !output.all_bounded
    error_message = "A role without the boundary is a mismatch."
  }

  expect_failures = [check.roles_carry_the_workload_boundary]
}

run "a_boundary_where_none_is_expected_is_reported" {
  command = plan

  variables {
    boundaries = ["arn:aws:iam::123456789012:policy/other"]
  }

  assert {
    condition     = !output.all_bounded
    error_message = "A role with another boundary is a mismatch."
  }

  expect_failures = [check.roles_carry_the_workload_boundary]
}
