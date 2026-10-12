mock_provider "aws" {
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::test-bucket-123" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
}

variables {
  name        = "test-bucket-123"
  kms_key_arn = "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000"
}

run "secure_defaults" {
  command = plan

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.this.block_public_acls,
      aws_s3_bucket_public_access_block.this.block_public_policy,
      aws_s3_bucket_public_access_block.this.ignore_public_acls,
      aws_s3_bucket_public_access_block.this.restrict_public_buckets,
    ])
    error_message = "All four public access block settings must be on."
  }

  assert {
    condition     = aws_s3_bucket_ownership_controls.this.rule[0].object_ownership == "BucketOwnerEnforced"
    error_message = "ACLs must be disabled (BucketOwnerEnforced)."
  }

  assert {
    condition = (
      one(one(aws_s3_bucket_server_side_encryption_configuration.this.rule).apply_server_side_encryption_by_default).sse_algorithm == "aws:kms"
      && one(aws_s3_bucket_server_side_encryption_configuration.this.rule).bucket_key_enabled
    )
    error_message = "Default encryption must be SSE-KMS with a bucket key."
  }

  assert {
    condition     = aws_s3_bucket_versioning.this.versioning_configuration[0].status == "Enabled"
    error_message = "Versioning must default to Enabled."
  }

  assert {
    condition     = !aws_s3_bucket.this.force_destroy
    error_message = "force_destroy must default to false."
  }

  assert {
    condition     = length(aws_s3_bucket_lifecycle_configuration.this) == 0 && length(aws_s3_bucket_cors_configuration.this) == 0
    error_message = "Lifecycle and CORS must be absent unless configured."
  }
}

run "sse_s3_without_a_kms_key" {
  command = plan

  variables {
    kms_key_arn = null
  }

  assert {
    condition = (
      one(one(aws_s3_bucket_server_side_encryption_configuration.this.rule).apply_server_side_encryption_by_default).sse_algorithm == "AES256"
      && !one(aws_s3_bucket_server_side_encryption_configuration.this.rule).bucket_key_enabled
    )
    error_message = "Without kms_key_arn, default encryption must be SSE-S3 (AES256) with no key."
  }
}

run "sse_kms_with_a_kms_key" {
  command = plan

  assert {
    condition = (
      one(one(aws_s3_bucket_server_side_encryption_configuration.this.rule).apply_server_side_encryption_by_default).kms_master_key_id == var.kms_key_arn
      && one(one(aws_s3_bucket_server_side_encryption_configuration.this.rule).apply_server_side_encryption_by_default).sse_algorithm == "aws:kms"
    )
    error_message = "With kms_key_arn, default encryption must use that key."
  }
}

run "tls_only_policy" {
  command = plan

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.bucket.statement :
      s.effect == "Deny" && anytrue([
        for c in s.condition : c.variable == "aws:SecureTransport" && contains(c.values, "false")
      ])
    ])
    error_message = "Bucket policy must deny requests without TLS."
  }
}

run "lifecycle_and_cors" {
  command = plan

  variables {
    lifecycle_rules = [{
      id                                     = "default"
      abort_incomplete_multipart_upload_days = 7
      noncurrent_version_transitions         = [{ days = 30, storage_class = "GLACIER_IR" }]
    }]
    cors_rules = [{
      allowed_methods = ["PUT"]
      allowed_origins = ["https://app.example.com"]
    }]
  }

  assert {
    condition     = length(aws_s3_bucket_lifecycle_configuration.this) == 1
    error_message = "Lifecycle configuration must be created when rules are given."
  }

  assert {
    condition     = one(aws_s3_bucket_cors_configuration.this[0].cors_rule).allowed_methods == toset(["PUT"])
    error_message = "CORS rule must be passed through."
  }
}

run "rejects_invalid_name" {
  command = plan

  variables {
    name = "Invalid_Bucket"
  }

  expect_failures = [var.name]
}
