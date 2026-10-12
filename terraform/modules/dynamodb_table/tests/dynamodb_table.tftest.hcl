mock_provider "aws" {}

variables {
  name        = "test-table"
  hash_key    = "pk"
  kms_key_arn = "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000"
  attributes = [
    { name = "pk", type = "S" },
    { name = "gk", type = "S" },
    { name = "sk", type = "S" },
  ]
}

run "defaults" {
  command = plan

  assert {
    condition     = aws_dynamodb_table.this.billing_mode == "PAY_PER_REQUEST"
    error_message = "Tables must be on-demand."
  }

  assert {
    condition     = aws_dynamodb_table.this.point_in_time_recovery[0].enabled
    error_message = "PITR must be enabled."
  }

  assert {
    condition = (
      aws_dynamodb_table.this.server_side_encryption[0].enabled
      && aws_dynamodb_table.this.server_side_encryption[0].kms_key_arn == var.kms_key_arn
    )
    error_message = "SSE must use the given customer-managed KMS key."
  }

  assert {
    condition     = aws_dynamodb_table.this.deletion_protection_enabled
    error_message = "Deletion protection must default to on."
  }

  assert {
    condition     = !aws_dynamodb_table.this.stream_enabled && output.stream_arn == null
    error_message = "Stream must be off unless a view type is given."
  }

  assert {
    condition     = length(aws_dynamodb_table.this.ttl) == 0
    error_message = "TTL must be off unless an attribute is given."
  }
}

run "stream_and_ttl_with_gsis" {
  command = plan

  variables {
    stream_view_type = "NEW_AND_OLD_IMAGES"
    ttl_attribute    = "expires_at_epoch"
    global_secondary_indexes = [
      { name = "gsi_all", hash_key = "gk", range_key = "sk" },
      { name = "gsi_include", hash_key = "sk", projection_type = "INCLUDE", non_key_attributes = ["a", "b"] },
    ]
  }

  assert {
    condition     = aws_dynamodb_table.this.stream_enabled && aws_dynamodb_table.this.stream_view_type == "NEW_AND_OLD_IMAGES"
    error_message = "Stream must be enabled with the given view type."
  }

  assert {
    condition     = one(aws_dynamodb_table.this.ttl).attribute_name == "expires_at_epoch" && one(aws_dynamodb_table.this.ttl).enabled
    error_message = "TTL must be enabled on the given attribute."
  }
}

run "rejects_include_without_attributes" {
  command = plan

  variables {
    global_secondary_indexes = [{ name = "bad", hash_key = "gk", projection_type = "INCLUDE" }]
  }

  expect_failures = [var.global_secondary_indexes]
}

run "rejects_unknown_stream_view_type" {
  command = plan

  variables {
    stream_view_type = "EVERYTHING"
  }

  expect_failures = [var.stream_view_type]
}
